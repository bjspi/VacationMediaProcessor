"""Shared typed data structures for Vacation Media Processor."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any


class MediaKind(str, Enum):
    """High-level media kind."""

    IMAGE = "image"
    VIDEO = "video"


class Confidence(str, Enum):
    """Confidence for resolved capture timestamps."""

    ZERO = "zero"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PlanStatus(str, Enum):
    """Analysis and planning status."""

    OK = "ok"
    WARN = "warn"
    SKIP = "skip"
    DONE = "done"


class ActionKind(str, Enum):
    """Supported planned action categories."""

    IMAGE_CONVERT = "image_convert"
    VIDEO_TRANSCODE = "video_transcode"
    VIDEO_DOWNSCALE = "video_downscale"
    WRITE_EXIF_FROM_FILENAME = "write_exif_from_filename"
    METADATA_NORMALIZE = "metadata_normalize"
    RENAME = "rename"
    BACKUP_ORIGINAL = "backup_original"
    REVIEW = "review"


class ApplyMode(str, Enum):
    """Metadata apply modes inherited from MediaTimeNormalizer."""

    RENAME_ONLY = "rename_only"
    SAMSUNG_CLEANUP = "samsung_cleanup"
    GPS_CLEANUP = "gps_cleanup"
    FULL_NORMALIZE = "full_normalize"


class Phase(str, Enum):
    """Pipeline phases shown in the GUI."""

    DISCOVERY = "discovery"
    METADATA_SCAN = "metadata_scan"
    PLANNING = "planning"
    IMAGE_CONVERSION = "image_conversion"
    JPEG_MAINTENANCE = "jpeg_maintenance"
    VIDEO_TRANSCODE = "video_transcode"
    METADATA_WRITE = "metadata_write"
    RENAME = "rename"
    MANIFEST = "manifest"


IMAGE_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".heic", ".heif"})
VIDEO_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".mov", ".m4v"})
SUPPORTED_EXTENSIONS: frozenset[str] = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


@dataclass(slots=True)
class ToolPaths:
    """External executable paths used by the processing pipeline."""

    exiftool: str = "exiftool"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    xnconvert: str = "nconvert"
    xnconvert_gui: str = r"C:\Program Files\XnConvert\xnconvert.exe"
    xnviewmp: str = r"C:\Program Files\XnViewMP\xnviewmp.exe"
    shutter_encoder: str = r"C:\Program Files\Shutter Encoder\Shutter Encoder.exe"


@dataclass(slots=True)
class DiffToolSettings:
    """External diff-tool command templates for image and video comparison."""

    image: str = ""
    video: str = ""
    text: str = ""


@dataclass(slots=True)
class VideoSettings:
    """Video transcoding settings."""

    fhd_crf: int = 25
    qhd_crf: int = 26
    uhd_crf: int = 28
    limit_to_fhd: bool = False
    qhd_long_edge_threshold: int = 2000
    uhd_long_edge_threshold: int = 3000
    preset: str = "medium"
    replace_if_larger: bool = False
    always_replace_below_mb: int = 50
    encoder: str = "libx265"
    audio_codec: str = "aac"  # "aac" | "ac3" | "copy"
    audio_bitrate: str = "160k"


@dataclass(slots=True)
class ImageSettings:
    """Image conversion settings."""

    jpeg_quality: int = 83
    heic_heif_to_jpg: bool = True
    heic_heif_jpeg_quality: int = 83
    skip_depth_heic_conversion: bool = False
    preserve_depth_as_gdepth: bool = False
    png_to_jpg: bool = False
    jpeg_rotate_by_exif: bool = True
    jpeg_rebuild_exif_thumbnail: bool = True
    parallel_workers: int = 8


@dataclass(slots=True)
class MetadataSettings:
    """Metadata normalization and cleanup settings."""

    cleanup_enabled: bool = True
    set_filesystem_dates: bool = True
    apply_mode: ApplyMode = ApplyMode.FULL_NORMALIZE
    stop_on_conflict: bool = True
    write_comment: bool = True
    sanity_tolerance_seconds: int = 180
    vacation_span_weeks: int = 6
    write_missing_from_filename_after_review: bool = True
    rename_format: str = "%Y%m%d_%H%M%S"
    rename_collision_use_subsec: bool = False
    out_of_range_marks_only: bool = True
    range_start: datetime | None = None
    range_end: datetime | None = None


@dataclass(slots=True)
class AppSettings:
    """User-configurable application settings."""

    recursive: bool = True
    language: str = "auto"  # 'auto' (system), 'de', 'en'
    folder_drop_behavior: str = "ask"  # 'ask', 'add', 'replace'
    skip_backup: bool = False
    read_after_exif: bool = False
    table_font_size: int = 10
    window_x: int | None = None
    window_y: int | None = None
    window_width: int = 1080
    window_height: int = 680
    main_window_geometry: str = ""
    lasso_window_geometry: str = ""
    pair_window_geometry: str = ""
    pair_viewer_geometry: str = ""
    lasso_load_target_after_move: bool = False
    lasso_thumbnail_cache_mode: str = "ram"
    lasso_thumbnail_workers: int = 8
    lasso_thumbnail_display_size: int = 132
    pair_check_workers: int = 8
    exiftool_read_batch_size: int = 20
    exiftool_parallel_batches: int = 1
    tools: ToolPaths = field(default_factory=ToolPaths)
    diff_tools: DiffToolSettings = field(default_factory=DiffToolSettings)
    images: ImageSettings = field(default_factory=ImageSettings)
    videos: VideoSettings = field(default_factory=VideoSettings)
    metadata: MetadataSettings = field(default_factory=MetadataSettings)


@dataclass(slots=True)
class MediaItem:
    """A discovered media file and its stable project-relative identity."""

    path: Path
    root: Path
    kind: MediaKind

    @property
    def suffix(self) -> str:
        """Return the lowercase file suffix."""
        return self.path.suffix.lower()

    @property
    def relative_path(self) -> Path:
        """Return the path relative to the selected root."""
        return self.path.relative_to(self.root)


@dataclass(slots=True)
class RawMetadata:
    """Raw ExifTool metadata payload for one file."""

    source_file: str
    tags: dict[str, Any]


@dataclass(slots=True)
class ResolvedTimestamp:
    """Resolved timestamp model used by planning and metadata writes."""

    local_dt: datetime | None
    utc_dt: datetime | None
    offset: timedelta | None
    confidence: Confidence
    source: str = ""
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    local_date_only: bool = False
    local_sources: tuple[str, ...] = ()
    offset_sources: tuple[str, ...] = ()
    utc_sources: tuple[str, ...] = ()


@dataclass(slots=True)
class AnalysisResult:
    """Complete analysis result for a media item."""

    item: MediaItem
    metadata: RawMetadata
    resolved: ResolvedTimestamp
    status: PlanStatus
    warnings: list[str] = field(default_factory=list)
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    has_depth: bool = False


@dataclass(slots=True)
class PlannedAction:
    """One planned filesystem, metadata, or conversion action."""

    kind: ActionKind
    description: str
    source: Path
    target: Path | None = None
    enabled: bool = True
    requires_review: bool = False


@dataclass(slots=True)
class MediaPlan:
    """Planned processing for one media item."""

    analysis: AnalysisResult
    actions: list[PlannedAction] = field(default_factory=list)
    final_path: Path | None = None


@dataclass(slots=True)
class PipelineProgress:
    """Progress event emitted by worker threads."""

    phase: Phase
    current: int
    total: int
    message: str


@dataclass(slots=True)
class ApplyItemUpdate:
    """Per-file apply result emitted as soon as one plan is finished."""

    run_id: str
    source_path: Path
    final_path: Path | None = None
    backup_path: Path | None = None
    changed: bool = False
    skipped: bool = False
    errors: list[str] = field(default_factory=list)
    original_size: int | None = None
    current_size: int | None = None


@dataclass(slots=True)
class PipelineReport:
    """Final report returned by a pipeline run."""

    run_id: str
    changed: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
