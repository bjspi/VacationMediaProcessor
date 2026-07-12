"""ExifTool metadata reading, per-file analysis, and write/cleanup tag plans.

The timestamp machinery lives in two focused modules and is re-exported here
as part of this package's public API:

* :mod:`vmp.timestamps.parsing` — pure string parsers/formatters
* :mod:`vmp.timestamps.resolution` — candidates, heuristics, sanity
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.logging_config import get_logger
from ..core.models import (
    AnalysisResult,
    MediaItem,
    MediaKind,
    MetadataSettings,
    PlanStatus,
    RawMetadata,
)
from ..core.processes import run_process

# Re-exported as public API — callers and tests import these names
# from vmp.metadata.
from ..timestamps.parsing import (  # noqa: F401
    EXIF_DT_RE,
    EXIF_DT_TZ_RE,
    FILENAME_DATE_ONLY_PATTERNS,
    FILENAME_DT_PATTERNS,
    ISO_DT_RE,
    OFFSET_RE,
    format_exif_datetime,
    format_iso_with_offset,
    format_iso_z,
    format_offset,
    format_offset_compact,
    get_first_str,
    has_any_tag,
    normalize_tz_suffix,
    parse_duration_seconds,
    parse_exif_datetime,
    parse_exif_datetime_with_offset,
    parse_filename_date_only,
    parse_filename_datetime,
    parse_filename_datetime_with_format,
    parse_gps_datetime,
    parse_iso_datetime,
    parse_offset,
    safe_make_datetime,
)
from ..timestamps.resolution import (  # noqa: F401
    LOCAL_EXIF_KEYS,
    OFFSET_KEYS,
    UTC_KEYS,
    ResolutionEvidence,
    TimeCandidate,
    TimeRole,
    build_time_candidates,
    choose_best_local,
    choose_best_offset,
    choose_best_utc,
    choose_date_only,
    collect_datetime_value_pool,
    evaluate_sanity,
    infer_from_all_datetime_values,
    infer_local_utc_offset_from_spread,
    is_core_metadata_datetime_key,
    is_datetime_like_key,
    is_ignored_datetime_source,
    is_offset_like_key,
    is_plausible_timezone_offset,
    is_utc_biased_key,
    max_spread_seconds,
    maybe_derive_offset,
    merge_duplicate_candidates,
    resolve_timestamp,
    seconds_between,
)
from ..core.models import ResolvedTimestamp  # noqa: F401  (re-export convenience)
from .gps import gps_coordinates, has_gps  # noqa: F401  (re-exported)
from .writing import (  # noqa: F401  (re-exported)
    GROUP_CLEANUP_TAGS,
    NON_WRITABLE_CLEANUP_TAGS,
    build_cleanup_tags,
    build_gps_datetime_cleanup_tags,
    build_normalizer_note,
    build_samsung_trailer_cleanup_tags,
    cleanup_tags_for_result,
    guess_offset_key,
    has_existing_normalizer_comment,
    metadata_write_tags,
    samsung_cleanup_tags_for_result,
)

LOGGER = get_logger(__name__)


READ_ARGS: tuple[str, ...] = (
    "-j",
    "-a",
    "-G1",
    "-s",
    "-time:all",
    "-File:all",
    "-EXIF:all",
    "-Composite:all",
    "-QuickTime:all",
    "-Keys:all",
    "-ItemList:all",
    "-XMP:all",
    "-MakerNotes:all",
    "-GPS:all",
    "-Samsung:all",
)


# Windows caps a child process command line at ~32k characters; stay well below.
_MAX_CMDLINE_CHARS = 24000


def read_metadata_batch(items: list[MediaItem], exiftool: str) -> dict[Path, RawMetadata]:
    """Read ExifTool JSON metadata for a batch of media items."""
    if not items:
        return {}
    # Large configured batch sizes combined with long paths can exceed the
    # Windows command-line limit and abort the whole scan; split transparently.
    paths_length = sum(len(str(item.path)) + 3 for item in items)
    if len(items) > 1 and paths_length > _MAX_CMDLINE_CHARS:
        mid = len(items) // 2
        records = read_metadata_batch(items[:mid], exiftool)
        records.update(read_metadata_batch(items[mid:], exiftool))
        return records
    args = [exiftool, *READ_ARGS, *(str(item.path) for item in items)]
    # A single unreadable/corrupt file in a batch must not abort the whole scan.
    # Run without raising on a non-zero exit, then parse whatever JSON ExifTool
    # produced: good files still resolve, and any file missing from the payload
    # degrades to a SKIP result upstream. An empty/unparseable payload leaves the
    # whole batch as SKIP rather than crashing the scan of every other file.
    result = run_process(args, check=False)
    if result.returncode != 0:
        LOGGER.warning(
            "ExifTool batch of %s file(s) exited with code %s; parsing partial output. stderr=%s",
            len(items),
            result.returncode,
            result.stderr.strip()[:2000],
        )
    payload: object = []
    stdout = result.stdout.strip()
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            LOGGER.warning(
                "ExifTool batch of %s file(s) produced unparseable JSON; treating batch as no-metadata.",
                len(items),
            )
            payload = []
    if not isinstance(payload, list):
        payload = []
    records: dict[Path, RawMetadata] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("SourceFile", ""))
        if not source:
            continue
        records[Path(source).resolve()] = RawMetadata(source_file=source, tags=dict(entry))
    return records


def analyze_item(item: MediaItem, raw: RawMetadata, settings: MetadataSettings) -> AnalysisResult:
    """Build an analysis result from raw metadata and user settings."""
    tolerance = settings.sanity_tolerance_seconds
    # Candidates and cross-value inference are pure functions of the raw tags;
    # compute them once and share them between resolve and sanity evaluation.
    candidates = build_time_candidates(raw, item.kind)
    inferred_all = infer_from_all_datetime_values(raw, tolerance)
    resolved = resolve_timestamp(item, raw, tolerance, candidates=candidates, inferred_all=inferred_all)
    status, warnings = evaluate_sanity(
        resolved, raw, item.kind, tolerance, candidates=candidates, inferred_all=inferred_all
    )
    warnings = [*resolved.warnings, *warnings]
    if resolved.local_dt and settings.range_start and resolved.local_dt < settings.range_start:
        warnings.append("Capture date is before the configured run date range.")
        status = PlanStatus.WARN
    if resolved.local_dt and settings.range_end and resolved.local_dt > settings.range_end:
        warnings.append("Capture date is after the configured run date range.")
        status = PlanStatus.WARN
    if has_existing_normalizer_comment(raw.tags, item.kind):
        status = PlanStatus.DONE
    tags = raw.tags
    width = _int_tag(tags, ("File:ImageWidth", "Composite:ImageWidth", "QuickTime:ImageWidth", "EXIF:ExifImageWidth"))
    height = _int_tag(tags, ("File:ImageHeight", "Composite:ImageHeight", "QuickTime:ImageHeight", "EXIF:ExifImageHeight"))
    # Only accept a real video compressor id/name here; the container type
    # (File:FileType, e.g. "MOV") is not a codec. When absent, codec stays None
    # so the pipeline enriches it with FFprobe's actual stream codec_name.
    codec = _str_tag(tags, ("QuickTime:CompressorID", "QuickTime:CompressorName"))
    has_depth = _detect_depth(tags)
    return AnalysisResult(item=item, metadata=raw, resolved=resolved, status=status, warnings=warnings, width=width, height=height, codec=codec, has_depth=has_depth)


def _detect_depth(tags: dict[str, Any]) -> bool:
    """Return True when the file carries an editable depth map / portrait blur.

    Detected cheaply from the ExifTool tags already read during the scan (no
    extra file decode). iPhone portrait shots expose ``XMP-depthData``/
    ``XMP-depthBlurEffect`` groups; Samsung portrait JPEGs expose
    ``Samsung:DepthMapData``.
    """
    for key in tags:
        if key.startswith(("XMP-depthData:", "XMP-depthBlurEffect:")):
            return True
        if key == "Samsung:DepthMapData" or key.endswith(":DepthMapData"):
            return True
    return False


def _int_tag(tags: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    """Return the first integer tag."""
    for key in keys:
        value = tags.get(key)
        try:
            return int(str(value))
        except (TypeError, ValueError):
            continue
    return None


def _str_tag(tags: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """Return the first non-empty string tag."""
    for key in keys:
        value = tags.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def has_exif_datetime_values(metadata: RawMetadata) -> bool:
    """Return True if raw metadata contains core EXIF capture dates."""
    return has_any_tag(metadata.tags, "Composite:SubSecDateTimeOriginal", "ExifIFD:DateTimeOriginal", "EXIF:DateTimeOriginal", "EXIF:CreateDate")


def missing_exif_suggestion(result: AnalysisResult) -> str:
    """Return a human-readable suggestion for a file without EXIF dates."""
    filename_dt, _ = parse_filename_datetime_with_format(result.item.path.name)
    if filename_dt is None:
        return "No filename datetime detected; keep unchanged or repair manually."
    value = format_exif_datetime(filename_dt)
    if result.item.kind == MediaKind.IMAGE:
        return f"Could write EXIF:DateTimeOriginal/EXIF:CreateDate/IFD0:ModifyDate={value}."
    return f"Could write container/file timestamps from filename datetime {value}."
