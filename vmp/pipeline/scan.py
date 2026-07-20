"""Scan phase: discover media, read metadata in batches, and build dry-run plans."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..core.discovery import discover_media, normalize_root
from ..core.i18n import tr
from ..tools import probe_video
from ..core.logging_config import get_logger
from ..metadata import analyze_item, read_metadata_batch
from ..core.models import (
    AnalysisResult,
    AppSettings,
    MediaItem,
    MediaKind,
    MediaPlan,
    Phase,
    PlanStatus,
    RawMetadata,
)
from .shared import (
    CancelCallback,
    ProgressCallback,
    ResultsCallback,
    _resolve_required_tool,
    emit,
    raise_if_cancelled,
)
from ..planner import build_plans

LOGGER = get_logger(__name__)


def scan_and_plan(
    root: Path,
    settings: AppSettings,
    callback: ProgressCallback | None = None,
    results_callback: ResultsCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> tuple[list[AnalysisResult], list[MediaPlan]]:
    """Discover files, read metadata, analyze timestamps, and build plans."""
    normalized_root = normalize_root(root)
    raise_if_cancelled(cancel_callback)
    LOGGER.info("Starting scan for %s", normalized_root)
    items = discover_media(normalized_root, recursive=settings.recursive)
    LOGGER.info("Discovered %s media files", len(items))
    emit(callback, Phase.DISCOVERY, len(items), len(items), f"Found {len(items)} media files.")
    return scan_items_and_plan(items, settings, callback, results_callback, cancel_callback)


def scan_items_and_plan(
    items: list[MediaItem],
    settings: AppSettings,
    callback: ProgressCallback | None = None,
    results_callback: ResultsCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> tuple[list[AnalysisResult], list[MediaPlan]]:
    """Scan and plan an already-discovered, potentially multi-root item list."""
    raise_if_cancelled(cancel_callback)
    _resolve_required_tool("ExifTool", settings.tools.exiftool)
    if any(item.kind == MediaKind.VIDEO for item in items):
        _resolve_required_tool("FFprobe", settings.tools.ffprobe)
    results: list[AnalysisResult] = []
    total = len(items)
    chunk_size = max(1, min(200, int(settings.exiftool_read_batch_size)))
    parallel_batches = max(1, min(8, int(settings.exiftool_parallel_batches)))
    chunks = [(index, items[index : index + chunk_size]) for index in range(0, total, chunk_size)]
    if parallel_batches <= 1 or len(chunks) <= 1:
        for index, chunk in chunks:
            raise_if_cancelled(cancel_callback)
            emit(
                callback,
                Phase.METADATA_SCAN,
                max(index, 1),
                total,
                tr("ExifTool liest Metadaten ({start}-{end} von {total})...").format(
                    start=index + 1, end=index + len(chunk), total=total
                ),
            )
            LOGGER.info("Reading metadata chunk %s-%s of %s", index + 1, index + len(chunk), total)
            raw_records = read_metadata_batch(chunk, settings.tools.exiftool)
            raise_if_cancelled(cancel_callback)
            results.extend(_analyze_metadata_chunk(chunk, raw_records, settings))
            if results_callback is not None:
                results_callback(results.copy(), build_plans(results, settings))
            emit(callback, Phase.METADATA_SCAN, min(index + len(chunk), total), total, "Metadata scan in progress...")
    else:
        emit(
            callback,
            Phase.METADATA_SCAN,
            1 if total else 0,
            total,
            tr("ExifTool liest Metadaten in {count} Batches ({parallel} parallel)...").format(
                count=len(chunks), parallel=parallel_batches
            ),
        )
        LOGGER.info(
            "Reading metadata in %s chunks of up to %s files with %s parallel ExifTool processes",
            len(chunks), chunk_size, parallel_batches,
        )
        completed: dict[int, tuple[int, list[MediaItem], dict[Path, RawMetadata]]] = {}
        next_flush = 0
        completed_files = 0
        with ThreadPoolExecutor(max_workers=parallel_batches) as executor:
            futures = {
                executor.submit(
                    _read_metadata_chunk,
                    chunk,
                    settings.tools.exiftool,
                    cancel_callback,
                ): (chunk_index, index, chunk)
                for chunk_index, (index, chunk) in enumerate(chunks)
            }
            for future in as_completed(futures):
                raise_if_cancelled(cancel_callback)
                chunk_index, index, chunk = futures[future]
                raw_records = future.result()
                completed[chunk_index] = (index, chunk, raw_records)
                completed_files += len(chunk)
                LOGGER.info("Metadata chunk %s-%s of %s finished", index + 1, index + len(chunk), total)
                while next_flush in completed:
                    _index, ready_chunk, ready_records = completed.pop(next_flush)
                    results.extend(_analyze_metadata_chunk(ready_chunk, ready_records, settings))
                    next_flush += 1
                if results_callback is not None:
                    results_callback(results.copy(), build_plans(results, settings))
                emit(callback, Phase.METADATA_SCAN, completed_files, total, "Metadata scan in progress...")
    raise_if_cancelled(cancel_callback)
    emit(callback, Phase.PLANNING, total, total, "Building dry-run plan...")
    plans = build_plans(results, settings)
    LOGGER.info("Item scan finished with %s results and %s plans", len(results), len(plans))
    return results, plans


def _read_metadata_chunk(
    chunk: list[MediaItem],
    exiftool: str,
    cancel_callback: CancelCallback | None,
) -> dict[Path, RawMetadata]:
    """Read one scheduled batch only if it has not been cancelled meanwhile."""
    raise_if_cancelled(cancel_callback)
    records = read_metadata_batch(chunk, exiftool)
    raise_if_cancelled(cancel_callback)
    return records



def _analyze_metadata_chunk(
    chunk: list[MediaItem],
    raw_records: dict[Path, RawMetadata],
    settings: AppSettings,
) -> list[AnalysisResult]:
    """Analyze one metadata chunk while preserving discovery order."""
    results: list[AnalysisResult] = []
    for item in chunk:
        raw = raw_records.get(item.path.resolve())
        if raw is None:
            results.append(_missing_metadata_result(item))
            continue
        result = analyze_item(item, raw, settings.metadata)
        if item.kind == MediaKind.VIDEO and (result.width is None or result.height is None or result.codec is None):
            _enrich_video_with_ffprobe(result, settings)
        results.append(result)
    return results



def _missing_metadata_result(item: MediaItem) -> AnalysisResult:
    """Create a skipped result when ExifTool returned no payload."""
    from ..core.models import Confidence, RawMetadata, ResolvedTimestamp

    return AnalysisResult(
        item=item,
        metadata=RawMetadata(source_file=str(item.path), tags={}),
        resolved=ResolvedTimestamp(None, None, None, Confidence.LOW, "", ["ExifTool returned no metadata."]),
        status=PlanStatus.SKIP,
        warnings=["ExifTool returned no metadata."],
    )



def _enrich_video_with_ffprobe(result: AnalysisResult, settings: AppSettings) -> None:
    """Fill missing video width, height, and codec fields from FFprobe."""
    try:
        payload = probe_video(result.item.path, settings)
    except Exception:  # noqa: BLE001
        return
    streams = payload.get("streams", [])
    if not isinstance(streams, list):
        return
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("codec_type") != "video":
            continue
        if result.width is None:
            result.width = _safe_int(stream.get("width"))
        if result.height is None:
            result.height = _safe_int(stream.get("height"))
        if result.codec is None and stream.get("codec_name"):
            result.codec = str(stream["codec_name"])
        return



def _safe_int(value: object) -> int | None:
    """Convert an object to int when possible."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


