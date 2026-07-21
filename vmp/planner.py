"""Build dry-run plans from analysis results."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .core.i18n import tr
from .core.models import (
    ActionKind,
    AnalysisResult,
    ApplyMode,
    AppSettings,
    MediaKind,
    MediaPlan,
    PlanStatus,
    PlannedAction,
)


_HEIC_SUFFIXES = {".heic", ".heif"}
_CONVERTIBLE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", *_HEIC_SUFFIXES}


def _converted_image_suffix(result: AnalysisResult, settings: AppSettings) -> str | None:
    """Return ``".jpg"`` when this image gets converted under Full normalize, else None.

    Single source of truth for the conversion decision: apply mode, media kind,
    the PNG/HEIC toggles, and the depth-HEIC keep rule all live here so
    :func:`final_extension` and :func:`needs_image_conversion` cannot drift apart.
    """
    if settings.metadata.apply_mode != ApplyMode.FULL_NORMALIZE:
        return None
    if result.item.kind != MediaKind.IMAGE:
        return None
    suffix = result.item.suffix
    if suffix not in _CONVERTIBLE_IMAGE_SUFFIXES:
        return None
    if suffix == ".png" and not settings.images.png_to_jpg:
        return None
    if suffix in _HEIC_SUFFIXES and not settings.images.heic_heif_to_jpg:
        return None
    if keeps_depth_heic(result, settings):
        return None
    return ".jpg"


def final_extension(result: AnalysisResult, settings: AppSettings) -> str:
    """Return the final extension for a planned media file."""
    suffix = result.item.suffix
    if settings.metadata.apply_mode != ApplyMode.FULL_NORMALIZE:
        return suffix
    if result.item.kind == MediaKind.VIDEO:
        return ".mp4"
    return _converted_image_suffix(result, settings) or suffix


def needs_image_conversion(result: AnalysisResult, settings: AppSettings) -> bool:
    """Return True when an image should be converted by XnConvert."""
    return _converted_image_suffix(result, settings) is not None


def keeps_depth_heic(result: AnalysisResult, settings: AppSettings) -> bool:
    """Return True when a depth-bearing HEIC/HEIF is kept native instead of converted.

    When the user enables "skip depth HEIC conversion", HEIC/HEIF files that carry
    an editable depth map (``result.has_depth``) are left as HEIC. They still get the
    normal capture-date normalization and timestamp rename; only the format conversion
    is skipped so the depth data survives.
    """
    if settings.metadata.apply_mode != ApplyMode.FULL_NORMALIZE:
        return False
    if not settings.images.skip_depth_heic_conversion:
        return False
    if result.item.kind != MediaKind.IMAGE:
        return False
    if result.item.suffix not in {".heic", ".heif"}:
        return False
    return bool(result.has_depth)


def needs_video_transcode(result: AnalysisResult, settings: AppSettings) -> bool:
    """Return True when a video should be transcoded."""
    if settings.metadata.apply_mode != ApplyMode.FULL_NORMALIZE:
        return False
    return result.item.kind == MediaKind.VIDEO


def needs_filename_exif_write(result: AnalysisResult, settings: AppSettings) -> bool:
    """Return True when Full normalize should visibly write filename-derived capture dates."""
    if settings.metadata.apply_mode != ApplyMode.FULL_NORMALIZE:
        return False
    resolved = result.resolved
    if resolved.local_dt is None or resolved.local_date_only:
        return False
    if "System:FileName" not in resolved.local_sources:
        return False
    real_local_sources = [source for source in resolved.local_sources if source != "System:FileName"]
    return not real_local_sources


def _image_conversion_description(result: AnalysisResult, settings: AppSettings) -> str:
    """Return a source-specific image conversion description."""
    suffix = result.item.suffix
    quality = settings.images.jpeg_quality
    if suffix in {".jpg", ".jpeg"}:
        return tr("JPEG-Re-Encode, Qualität {quality}.").format(quality=quality)
    if suffix == ".heic":
        return tr("HEIC -> JPEG, Qualität {quality}.").format(quality=settings.images.heic_heif_jpeg_quality)
    if suffix == ".heif":
        return tr("HEIF -> JPEG, Qualität {quality}.").format(quality=settings.images.heic_heif_jpeg_quality)
    return tr("{format} -> JPEG, Qualität {quality}.").format(format=suffix.removeprefix(".").upper(), quality=quality)


def _metadata_normalize_description(settings: AppSettings) -> str:
    """Return the metadata-normalization action text for current settings."""
    if settings.metadata.set_filesystem_dates:
        return tr("Metadaten normalisieren, Dateisystem-Zeitstempel setzen, Junk-Tags bereinigen.")
    return tr("Metadaten normalisieren und Junk-Tags bereinigen.")


def _audio_transcode_description(settings: AppSettings) -> str:
    """Return the planned audio handling text for video transcode actions."""
    codec = settings.videos.audio_codec
    if codec == "copy":
        return tr("Audio-Kopie, falls MP4-kompatibel.")
    label = "AC3" if codec == "ac3" else "AAC"
    return tr("Audio -> {label} {bitrate}.").format(label=label, bitrate=settings.videos.audio_bitrate)


def video_bucket(result: AnalysisResult, settings: AppSettings) -> str:
    """Return the source video resolution bucket."""
    width = result.width or 0
    height = result.height or 0
    long_edge = max(width, height)
    qhd_threshold = max(720, int(settings.videos.qhd_long_edge_threshold))
    uhd_threshold = max(qhd_threshold + 1, int(settings.videos.uhd_long_edge_threshold))
    if long_edge >= uhd_threshold:
        return "4K"
    if long_edge >= qhd_threshold:
        return "QHD"
    return "FHD"


def effective_video_bucket(result: AnalysisResult, settings: AppSettings) -> str:
    """Return the bucket used for encoding settings after optional downscaling."""
    if video_downscale_target(result, settings) is not None:
        return "FHD"
    return video_bucket(result, settings)


def video_bucket_label(result: AnalysisResult, settings: AppSettings) -> str:
    """Return a visible source/effective bucket label."""
    source_bucket = video_bucket(result, settings)
    effective_bucket = effective_video_bucket(result, settings)
    if source_bucket == effective_bucket:
        return source_bucket
    return f"{source_bucket} -> {effective_bucket}"


def crf_for_video(result: AnalysisResult, settings: AppSettings) -> int:
    """Return the configured CRF for a video analysis result."""
    bucket = effective_video_bucket(result, settings)
    if bucket == "4K":
        return settings.videos.uhd_crf
    if bucket == "QHD":
        return settings.videos.qhd_crf
    return settings.videos.fhd_crf


def video_downscale_target(result: AnalysisResult, settings: AppSettings) -> tuple[int, int] | None:
    """Return the maximum output size when Full HD limiting is enabled."""
    if not settings.videos.limit_to_fhd:
        return None
    if result.item.kind != MediaKind.VIDEO:
        return None
    width = result.width or 0
    height = result.height or 0
    if width <= 0 or height <= 0:
        return None
    if width <= 1920 and height <= 1080:
        return None
    return 1920, 1080


def video_fps_limit(result: AnalysisResult, settings: AppSettings) -> int | None:
    """Return a 30-fps output cap only for source videos above that rate."""
    if not settings.videos.limit_to_30_fps:
        return None
    if result.item.kind != MediaKind.VIDEO or result.fps is None:
        return None
    # Tolerate tiny probe/float deviations around an actual 30-fps source.
    return 30 if result.fps > 30.01 else None


def resolve_collision_target(
    parent: Path,
    base_dt: datetime,
    rename_format: str,
    extension: str,
    source_path: Path,
    reserved: set[Path],
    *,
    microsecond: int = 0,
    use_subsec: bool = False,
) -> Path:
    """Find a collision-free target path without changing the capture time.

    If the desired target already exists but is the same file being processed,
    it is returned as-is. On collision the fallback is normally ``-2``, ``-3``, ...
    suffixes. When ``use_subsec`` is set and the capture time carries sub-second
    precision (``microsecond`` > 0), a millisecond suffix like ``.450ms`` is tried
    first, giving a more meaningful disambiguator than a running index.
    """
    source_resolved = source_path.resolve()
    base = base_dt.strftime(rename_format)

    def _take(candidate: Path) -> Path | None:
        if candidate.resolve() == source_resolved:
            reserved.add(candidate)
            return candidate
        if candidate not in reserved and not candidate.exists():
            reserved.add(candidate)
            return candidate
        return None

    primary = _take(parent / f"{base}{extension}")
    if primary is not None:
        return primary
    if use_subsec and microsecond:
        milliseconds = microsecond // 1000
        subsec = _take(parent / f"{base}.{milliseconds:03d}ms{extension}")
        if subsec is not None:
            return subsec
    index = 2
    while True:
        indexed = _take(parent / f"{base}-{index}{extension}")
        if indexed is not None:
            return indexed
        index += 1


def rename_target(
    result: AnalysisResult,
    settings: AppSettings,
    extension: str,
    reserved: set[Path],
) -> Path | None:
    """Build the final timestamp-based target path."""
    local_dt = result.resolved.local_dt
    if local_dt is None or result.resolved.local_date_only:
        return None
    target = resolve_collision_target(
        result.item.path.parent,
        local_dt,
        settings.metadata.rename_format,
        extension,
        result.item.path,
        reserved,
        microsecond=local_dt.microsecond,
        use_subsec=settings.metadata.rename_collision_use_subsec,
    )
    return target


def build_plans(results: list[AnalysisResult], settings: AppSettings) -> list[MediaPlan]:
    """Build dry-run media plans."""
    reserved: set[Path] = set()
    plans: list[MediaPlan] = []
    for result in results:
        extension = final_extension(result, settings)
        target = rename_target(result, settings, extension, reserved)
        actions: list[PlannedAction] = []

        if result.status == PlanStatus.SKIP:
            actions.append(
                PlannedAction(
                    kind=ActionKind.REVIEW,
                    description=tr("Prüfen: kein verwertbarer Aufnahmezeitpunkt."),
                    source=result.item.path,
                    enabled=False,
                    requires_review=True,
                )
            )
            plans.append(MediaPlan(analysis=result, actions=actions, final_path=None))
            continue

        if result.resolved.local_date_only:
            actions.append(
                PlannedAction(
                    kind=ActionKind.REVIEW,
                    description=tr("Prüfen: nur ein Datum gefunden; keine echte Aufnahmezeit."),
                    source=result.item.path,
                    enabled=False,
                    requires_review=True,
                )
            )
            actions.append(
                PlannedAction(
                    kind=ActionKind.METADATA_NORMALIZE,
                    description=tr("Nur Cleanup; kein Zeitstempel-Schreiben und kein Rename für Datum-only-Dateien."),
                    source=result.item.path,
                    target=result.item.path,
                )
            )
            plans.append(MediaPlan(analysis=result, actions=actions, final_path=result.item.path))
            continue

        if result.status == PlanStatus.DONE:
            actions.append(
                PlannedAction(
                    kind=ActionKind.METADATA_NORMALIZE,
                    description=tr("Bereits von VMP verarbeitet; nur Cleanup (idempotent)."),
                    source=result.item.path,
                    target=result.item.path,
                )
            )
            plans.append(MediaPlan(analysis=result, actions=actions, final_path=result.item.path))
            continue

        if needs_image_conversion(result, settings):
            actions.append(
                PlannedAction(
                    kind=ActionKind.IMAGE_CONVERT,
                    description=_image_conversion_description(result, settings),
                    source=result.item.path,
                    target=target,
                )
            )

        if needs_video_transcode(result, settings):
            bucket = video_bucket_label(result, settings)
            crf = crf_for_video(result, settings)
            downscale_target = video_downscale_target(result, settings)
            fps_limit = video_fps_limit(result, settings)
            if downscale_target is not None:
                actions.append(
                    PlannedAction(
                        kind=ActionKind.VIDEO_DOWNSCALE,
                        description=tr("Video auf Full HD herunterskalieren, Seitenverhältnis bleibt erhalten."),
                        source=result.item.path,
                        target=target,
                    )
                )
            actions.append(
                PlannedAction(
                    kind=ActionKind.VIDEO_TRANSCODE,
                    description=(
                        tr("Transcode zu HEVC/x265 MP4, Bucket {bucket}, CRF {crf}").format(bucket=bucket, crf=crf)
                        + (
                            "; "
                            + ", ".join(
                                option
                                for option in (
                                    tr("Full-HD-Downscale") if downscale_target is not None else "",
                                    tr("max. 30 fps") if fps_limit is not None else "",
                                )
                                if option
                            )
                            + "; "
                            if downscale_target is not None or fps_limit is not None
                            else "; "
                        )
                        + _audio_transcode_description(settings)
                    ),
                    source=result.item.path,
                    target=target,
                )
            )

        if needs_filename_exif_write(result, settings):
            actions.append(
                PlannedAction(
                    kind=ActionKind.WRITE_EXIF_FROM_FILENAME,
                    description=tr("Aufnahmedatum aus dem Dateinamen in EXIF/Container-Metadaten schreiben."),
                    source=result.item.path,
                    target=target,
                )
            )

        metadata_description = _metadata_normalize_description(settings)
        if keeps_depth_heic(result, settings):
            metadata_description = tr("HEIC bleibt HEIC (Tiefendaten erhalten), nur Rename. {rest}").format(rest=metadata_description)
        actions.append(
            PlannedAction(
                kind=ActionKind.METADATA_NORMALIZE,
                description=metadata_description,
                source=result.item.path,
                target=target,
            )
        )
        if target is not None and target != result.item.path:
            actions.append(
                PlannedAction(
                    kind=ActionKind.RENAME,
                    description=tr("Umbenennen zu {name}.").format(name=target.name),
                    source=result.item.path,
                    target=target,
                )
            )

        plans.append(MediaPlan(analysis=result, actions=actions, final_path=target))
    return plans
