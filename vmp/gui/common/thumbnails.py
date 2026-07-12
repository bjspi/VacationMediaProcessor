"""Shared thumbnail decoding and caching for the GUI dialogs.

Decoding (JPEG/PNG via QImageReader, HEIC via pillow-heif, video frames via
ffmpeg) is safe to run off the GUI thread because only ``QImage`` — never
``QPixmap`` — is produced here. The :class:`ThumbnailService` feeds a bounded
worker pool from a queue and marshals results back through a Qt signal.
"""

from __future__ import annotations

import hashlib
import logging
import queue
import subprocess
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QImageReader

from ...core.processes import NO_WINDOW_CREATIONFLAGS, resolve_executable

LOGGER = logging.getLogger("vmp.gui.common.thumbnails")

THUMB_DECODE_SIZE = 240  # decode resolution (>= max slider size) so enlarging stays crisp
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v"}
HEIC_SUFFIXES = {".heic", ".heif"}

# Shared in-RAM cache across dialogs, bounded so a 10k-photo library cannot
# grow it to gigabytes over a long GUI session (LRU by byte size).
_THUMB_MEMORY_CACHE_MAX_BYTES = 256 * 1024 * 1024

_heif_registered = False


def load_oriented_qimage(path: Path, max_size: int | None = None) -> QImage | None:
    """Load a JPEG/PNG as a QImage with EXIF orientation applied.

    Uses QImageReader's auto-transform so Qt rotates the image from its own EXIF
    orientation tag (matching how IrfanView/Explorer show it), independent of how
    exiftool formats the tag value. Optionally scaled down to ``max_size`` px.
    """
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    if max_size is not None:
        # Let the decoder produce the small image directly instead of fully
        # materializing a 48 MP photo and scaling afterwards.
        source = reader.size()
        if source.isValid() and source.width() > 0 and source.height() > 0:
            scaled = source.scaled(max_size, max_size, Qt.AspectRatioMode.KeepAspectRatio)
            if scaled.width() < source.width() or scaled.height() < source.height():
                reader.setScaledSize(scaled)
    image = reader.read()
    if image.isNull():
        return None
    if max_size is not None and (image.width() > max_size or image.height() > max_size):
        image = image.scaled(
            max_size, max_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
    return image


def heic_to_qimage(path: Path, size: int) -> QImage | None:
    """Decode a HEIC/HEIF file to a scaled QImage via pillow-heif."""
    global _heif_registered
    try:
        import pillow_heif
        from PIL import Image

        if not _heif_registered:
            pillow_heif.register_heif_opener()
            _heif_registered = True
        with Image.open(str(path)) as im:
            im = im.convert("RGBA")
            im.thumbnail((size, size))
            qimage = QImage(im.tobytes("raw", "RGBA"), im.width, im.height, QImage.Format.Format_RGBA8888)
            return qimage.copy()
    except Exception:  # noqa: BLE001
        LOGGER.debug("HEIC thumbnail failed for %s", path, exc_info=True)
        return None


def video_frame_thumb(path: Path, ffmpeg: str | None, size: int, seek: str | None = "1") -> QImage | None:
    """Grab a single frame as a scaled QImage via ffmpeg.

    ``seek`` positions video inputs one second in; pass ``None`` for still-image
    inputs (e.g. the HEIC fallback decode), where a seek would fail.
    """
    resolved = resolve_executable(ffmpeg) if ffmpeg else None
    if resolved is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        args = [resolved, "-hide_banner", "-y"]
        if seek:
            args += ["-ss", seek]
        scale = f"scale='min({size},iw)':'min({size},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
        args += ["-i", str(path), "-frames:v", "1", "-vf", scale, str(tmp_path)]
        subprocess.run(args, capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW_CREATIONFLAGS)
        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            return None
        image = QImage(str(tmp_path))
        return image if not image.isNull() else None
    except Exception:  # noqa: BLE001
        LOGGER.debug("Video thumbnail failed for %s", path, exc_info=True)
        return None
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def decode_thumb(path: Path, kind: str, ffmpeg: str | None, size: int) -> QImage | None:
    """Decode a small thumbnail QImage for an image or video (off-GUI-thread safe)."""
    suffix = path.suffix.lower()
    if kind == "video" or suffix in VIDEO_SUFFIXES:
        return video_frame_thumb(path, ffmpeg, size)
    if suffix in HEIC_SUFFIXES:
        return heic_to_qimage(path, size)
    return load_oriented_qimage(path, max_size=size)


class ThumbRelay(QObject):
    """Marshals decoded thumbnails back to the GUI thread."""

    ready = pyqtSignal(str, object, int)  # path-string, QImage | None, selection token


def thumb_cache_key(path: Path, kind: str, size: int) -> tuple[str, int, int, int, str]:
    """Return an invalidation-safe thumbnail cache key."""
    try:
        stat = path.stat()
        mtime_ns = stat.st_mtime_ns
        file_size = stat.st_size
    except OSError:
        mtime_ns = 0
        file_size = -1
    try:
        path_key = str(path.resolve()).casefold()
    except OSError:
        path_key = str(path).casefold()
    return (path_key, mtime_ns, file_size, size, kind)


def _thumb_disk_path(key: tuple[str, int, int, int, str]) -> Path:
    """Return the disk-cache path for a thumbnail key."""
    digest = hashlib.sha256("|".join(map(str, key)).encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / "VacationMediaProcessor" / "thumbs" / f"{digest}.png"


_MISSING = object()


class _LruImageCache:
    """Byte-bounded, thread-safe LRU cache for decoded thumbnails.

    Stored values may be ``None`` (a failed decode is cached so it is not
    retried on every dialog open); ``None`` entries are charged a nominal cost.
    """

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._bytes = 0
        self._items: OrderedDict[tuple, QImage | None] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _cost(image: QImage | None) -> int:
        return int(image.sizeInBytes()) if isinstance(image, QImage) and not image.isNull() else 64

    def get(self, key: tuple) -> QImage | None:
        with self._lock:
            if key not in self._items:
                return None
            self._items.move_to_end(key)
            return self._items[key]

    def __contains__(self, key: tuple) -> bool:
        with self._lock:
            return key in self._items

    def put(self, key: tuple, image: QImage | None) -> None:
        cost = self._cost(image)
        with self._lock:
            # A sentinel default: a stored None (cached failed decode) must be
            # un-charged too, otherwise re-puts leak accounting bytes.
            old = self._items.pop(key, _MISSING)
            if old is not _MISSING:
                self._bytes -= self._cost(old)
            self._items[key] = image
            self._bytes += cost
            while self._bytes > self._max_bytes and len(self._items) > 1:
                _evicted_key, evicted = self._items.popitem(last=False)
                self._bytes -= self._cost(evicted)

    def __setitem__(self, key: tuple, image: QImage | None) -> None:
        self.put(key, image)


_SHARED_THUMB_MEMORY_CACHE = _LruImageCache(_THUMB_MEMORY_CACHE_MAX_BYTES)


class ThumbnailService:
    """Bounded background decoder feeding thumbnails to a review strip."""

    def __init__(
        self,
        ffmpeg: str | None,
        relay: ThumbRelay,
        size: int = THUMB_DECODE_SIZE,
        workers: int = 3,
        cache_mode: str = "ram",
    ) -> None:
        self._ffmpeg = ffmpeg
        self._relay = relay
        self._size = size
        self._cache_mode = cache_mode if cache_mode in {"ram", "disk", "off"} else "ram"
        self._memory_cache = _SHARED_THUMB_MEMORY_CACHE if self._cache_mode in {"ram", "disk"} else _LruImageCache(1)
        self._queue: queue.Queue[tuple[Path, str, int]] = queue.Queue()
        self._stop = False
        self._current_token = 0
        # workers=0 yields a cache-only service (used by tests); the dialogs
        # clamp their configured worker count to >= 1.
        self._threads = [threading.Thread(target=self._run, daemon=True) for _ in range(max(0, int(workers)))]
        for thread in self._threads:
            thread.start()

    def submit(self, path: Path, kind: str, token: int) -> None:
        """Queue a thumbnail for decoding."""
        self._current_token = max(self._current_token, token)
        self._queue.put((path, kind, token))

    def invalidate_before(self, token: int) -> None:
        """Mark all queued jobs older than ``token`` as stale (skipped, not decoded)."""
        self._current_token = max(self._current_token, token)

    def cached(self, path: Path, kind: str) -> QImage | None:
        """Return an in-RAM cached thumbnail if immediately available.

        Deliberately does *not* consult the disk cache: that would add per-item
        stat/decode I/O on the GUI thread; disk hits flow through the workers.
        """
        if self._cache_mode not in {"ram", "disk"}:
            return None
        cached = self._memory_cache.get(thumb_cache_key(path, kind, self._size))
        if isinstance(cached, QImage) and not cached.isNull():
            return cached
        return None

    def stop(self) -> None:
        """Signal worker threads to exit and drop all pending jobs."""
        self._stop = True
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def _run(self) -> None:
        while not self._stop:
            try:
                path, kind, token = self._queue.get(timeout=0.3)
            except queue.Empty:
                continue
            if self._stop:
                break
            if token < self._current_token:
                continue  # stale job from a superseded selection; skip the decode
            image = None
            key = thumb_cache_key(path, kind, self._size)
            try:
                if self._cache_mode in {"ram", "disk"} and key in self._memory_cache:
                    # A cached None means the decode failed before — emit the
                    # fallback instead of re-decoding on every dialog open.
                    self._relay.ready.emit(str(path), self._memory_cache.get(key), token)
                    continue
                if self._cache_mode == "disk":
                    disk_path = _thumb_disk_path(key)
                    if disk_path.exists():
                        cached = QImage(str(disk_path))
                        if not cached.isNull():
                            image = cached
                if image is None:
                    image = decode_thumb(path, kind, self._ffmpeg, self._size)
                    if self._cache_mode == "disk" and isinstance(image, QImage) and not image.isNull():
                        disk_path = _thumb_disk_path(key)
                        disk_path.parent.mkdir(parents=True, exist_ok=True)
                        image.save(str(disk_path), "PNG")
                if self._cache_mode in {"ram", "disk"}:
                    self._memory_cache.put(key, image)
            except Exception:  # noqa: BLE001
                LOGGER.debug("Thumbnail decode error for %s", path, exc_info=True)
            self._relay.ready.emit(str(path), image, token)
