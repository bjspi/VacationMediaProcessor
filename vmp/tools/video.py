"""FFmpeg/FFprobe wrappers: probing, audio strategy, and HEVC transcoding."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Callable

from ..core.logging_config import get_logger
from ..core.models import AppSettings
from ..core.processes import (
    NO_WINDOW_CREATIONFLAGS,
    ExternalToolError,
    register_process,
    run_process,
    unregister_process,
)

LOGGER = get_logger(__name__)


# Per-file FFprobe cache. A single video is probed several times across a run
# (scan enrichment, apply duration precompute, audio-codec check, transcode
# progress); each probe spawns ffprobe and costs real time. Cache the parsed
# result keyed by (resolved path, mtime, size) so repeated probes of the same
# unchanged file are free, while any rewrite of the file (new mtime/size) misses
# the cache and re-probes automatically.
_PROBE_CACHE: dict[tuple[str, int, int], dict[str, object]] = {}

_PROBE_CACHE_LOCK = Lock()

# A long-lived GUI session keeps scanning new trees; bound the cache so stale
# entries (old mtimes, moved files) cannot accumulate ffprobe payloads forever.
_PROBE_CACHE_MAX_ENTRIES = 4096



def _probe_cache_key(path: Path) -> tuple[str, int, int] | None:
    """Return a cache key that changes whenever the file content changes."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path.resolve()), stat.st_mtime_ns, stat.st_size)



# Audio codecs that play back reliably inside an MP4 container, so a stream copy
# is safe. Anything else (PCM, FLAC, Vorbis, ...) muxes into MP4 without ffmpeg
# erroring but plays back broken/silently in QuickTime/browsers, so it must be
# re-encoded instead of copied.
MP4_COPY_SAFE_AUDIO_CODECS: frozenset[str] = frozenset(
    {"aac", "ac3", "eac3", "mp3", "alac"}
)



def _probe_audio_codec(path: Path, settings: AppSettings) -> str | None:
    """Return the first audio stream's codec name, or None when unavailable."""
    try:
        payload = probe_video(path, settings)
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not probe audio codec for %s; assuming stream copy is safe.", path)
        return None
    streams = payload.get("streams", [])
    if not isinstance(streams, list):
        return None
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == "audio":
            name = stream.get("codec_name")
            return str(name).lower() if name else None
    return None



def _audio_transcode_args(source: Path, settings: AppSettings) -> list[str]:
    """Return ffmpeg audio arguments, forcing AAC when a copy would be unplayable.

    ``copy`` keeps the original track only when it is an MP4-safe codec. A PCM or
    other MP4-awkward track would otherwise be stream-copied into a technically
    valid file that many players cannot play, and that file would then replace the
    original — so such tracks are transparently re-encoded to AAC instead.
    """
    if settings.videos.audio_codec != "copy":
        return ["-c:a", settings.videos.audio_codec, "-b:a", settings.videos.audio_bitrate]
    source_codec = _probe_audio_codec(source, settings)
    if source_codec is None or source_codec in MP4_COPY_SAFE_AUDIO_CODECS:
        return ["-c:a", "copy"]
    LOGGER.warning(
        "Source audio codec '%s' is not MP4-safe for stream copy; re-encoding to AAC %s: %s",
        source_codec,
        settings.videos.audio_bitrate,
        source,
    )
    return ["-c:a", "aac", "-b:a", settings.videos.audio_bitrate]



def probe_video(path: Path, settings: AppSettings) -> dict[str, object]:
    """Read FFprobe stream metadata for a video file (cached per file state).

    The returned dict is treated as read-only by callers; it may be a shared
    cached instance, so do not mutate it in place.
    """
    key = _probe_cache_key(path)
    if key is not None:
        with _PROBE_CACHE_LOCK:
            cached = _PROBE_CACHE.get(key)
        if cached is not None:
            LOGGER.info("Using cached FFprobe metadata path=%s", path)
            return cached
    LOGGER.info("Probing video metadata path=%s", path)
    args = [
        settings.tools.ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    result = run_process(args)
    payload = dict(json.loads(result.stdout))
    if key is not None:
        with _PROBE_CACHE_LOCK:
            while len(_PROBE_CACHE) >= _PROBE_CACHE_MAX_ENTRIES:
                _PROBE_CACHE.pop(next(iter(_PROBE_CACHE)))
            _PROBE_CACHE[key] = payload
    return payload



def transcode_video(
    source: Path,
    target: Path,
    crf: int,
    settings: AppSettings,
    max_dimensions: tuple[int, int] | None = None,
    progress_fn: Callable[[float, float, str], None] | None = None,
) -> None:
    """Transcode a video to HEVC/x265 MP4.

    When ``progress_fn`` is provided, it is called with
    ``(current_seconds, total_seconds, speed_text)`` as FFmpeg reports progress,
    enabling the GUI to show per-file progress and ETA.
    """
    import re
    import subprocess

    target.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info(
        "Transcoding video source=%s target=%s encoder=%s preset=%s crf=%s",
        source,
        target,
        settings.videos.encoder,
        settings.videos.preset,
        crf,
    )
    total_duration = 0.0
    if progress_fn is not None:
        try:
            probe = probe_video(source, settings)
            fmt = probe.get("format", {})
            if isinstance(fmt, dict):
                total_duration = float(fmt.get("duration", 0.0))
        except Exception:  # noqa: BLE001
            LOGGER.warning("Could not probe duration for progress, ETA will be unavailable: %s", source)
    args = [
        settings.tools.ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
        "-map",
        "-0:d",
        "-ignore_unknown",
        "-c:v",
        settings.videos.encoder,
        "-preset",
        settings.videos.preset,
        "-crf",
        str(crf),
        "-tag:v",
        "hvc1",
    ]
    args.extend(_audio_transcode_args(source, settings))
    args.extend(
        [
            "-c:s",
            "copy",
            "-map_metadata",
            "0",
            "-movflags",
            "use_metadata_tags+faststart",
        ]
    )
    video_filters = ["format=yuv420p"]
    if max_dimensions is not None:
        max_width, max_height = max_dimensions
        video_filters.insert(
            0,
            f"scale='min({max_width},iw)':'min({max_height},ih)':force_original_aspect_ratio=decrease",
        )
    args.extend(["-vf", ",".join(video_filters)])
    if progress_fn is not None:
        args.extend(["-progress", "pipe:1"])
    args.append(str(target))
    LOGGER.info("FFmpeg command: %s", subprocess.list2cmdline(args))
    if progress_fn is None:
        run_process(args)
        return
    time_re = re.compile(r"out_time_us=(\d+)")
    speed_re = re.compile(r"speed=\s*([\d.]+x)")

    import threading

    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)

    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW_CREATIONFLAGS,
        )
    except OSError:
        LOGGER.exception("External process could not be started: %s", " ".join(args))
        raise
    register_process(proc)
    assert proc.stdout is not None
    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()
    current_sec = 0.0
    speed_text = ""
    try:
        for line in proc.stdout:
            line = line.strip()
            m = time_re.match(line)
            if m:
                current_sec = int(m.group(1)) / 1_000_000.0
            ms = speed_re.match(line)
            if ms:
                speed_text = ms.group(1)
            if line == "progress=end":
                progress_fn(total_duration or current_sec, total_duration, speed_text)
            elif line == "progress=continue" and total_duration > 0:
                progress_fn(current_sec, total_duration, speed_text)
        proc.wait()
    finally:
        # If the progress callback or the pipe read raised, ffmpeg would keep
        # encoding invisibly after unregistering — kill it before letting go.
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        unregister_process(proc)
    stderr_thread.join(timeout=5.0)
    stderr_tail = "".join(stderr_chunks).strip()[-4000:]
    LOGGER.info("External process finished with return code %s", proc.returncode)
    if stderr_tail:
        LOGGER.info("External process stderr: %s", stderr_tail)
    if proc.returncode != 0:
        command = " ".join(args)
        LOGGER.error("External process failed (%s): %s", proc.returncode, command)
        raise ExternalToolError(
            f"Command failed ({proc.returncode}): {command}\n{stderr_tail}"
        )


