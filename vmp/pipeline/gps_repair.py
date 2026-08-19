"""Standalone, backup-aware GPS metadata repair workflow."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from ..core.logging_config import get_logger
from ..core.models import AppSettings, MediaItem, Phase, PipelineProgress
from ..core.processes import run_process
from ..gps_repair import (
    GpsAssignment,
    GpsRepairEntryResult,
    GpsRepairReport,
    haversine_km,
    is_valid_position,
)
from ..manifest import json_default
from ..metadata import gps_coordinates, read_metadata_batch
from ..tools import _set_windows_creation_time
from .shared import CancelCallback, ProgressCallback, backup_dir, make_run_id

LOGGER = get_logger(__name__)
READBACK_TOLERANCE_METRES = 2.0

# ExifTool writes the QuickTime ``Keys`` location atom only in real QuickTime
# containers. HEIC/HEIF are ISO-BMFF based too, but ExifTool refuses the Keys
# group for them: it prints "0 image files updated" and still exits 0, so the
# write looks successful and only the readback would notice. Those still images
# therefore go through the EXIF GPS tags, which is also what every photo viewer
# reads from a HEIC.
_QUICKTIME_LOCATION_SUFFIXES = {".mp4", ".mov", ".m4v"}

# ExifTool reports a refused write on stdout instead of via its exit status.
_EXIFTOOL_NO_UPDATE_RE = re.compile(r"^\s*0 image files updated", re.MULTILINE)


def _iso6709(latitude: float, longitude: float) -> str:
    """Return a signed ISO-6709 location without invented altitude."""
    return f"{latitude:+010.6f}{longitude:+011.6f}/"


def gps_write_arguments(path: Path, latitude: float, longitude: float) -> list[str]:
    """Return format-appropriate ExifTool assignments for one coordinate."""
    if path.suffix.lower() in _QUICKTIME_LOCATION_SUFFIXES:
        return [f"-Keys:GPSCoordinates={_iso6709(latitude, longitude)}"]
    return [
        f"-EXIF:GPSLatitude={abs(latitude):.8f}",
        f"-EXIF:GPSLatitudeRef={'N' if latitude >= 0 else 'S'}",
        f"-EXIF:GPSLongitude={abs(longitude):.8f}",
        f"-EXIF:GPSLongitudeRef={'E' if longitude >= 0 else 'W'}",
    ]


def _require_exiftool_update(stdout: str, path: Path) -> None:
    """Turn ExifTool's silent "nothing written" report into a real error.

    A refused tag group leaves the file untouched with a zero exit status. The
    readback would catch it, but only as a confusing coordinate mismatch, so
    name the actual cause here instead.
    """
    if _EXIFTOOL_NO_UPDATE_RE.search(stdout or ""):
        raise RuntimeError(
            f"ExifTool hat {path.name} nicht verändert (0 image files updated); "
            "das Dateiformat akzeptiert die verwendeten GPS-Tags nicht."
        )


def _relative_or_name(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return Path(path.name)


def _read_one(assignment: GpsAssignment, settings: AppSettings):
    item = MediaItem(
        path=assignment.target.path,
        root=assignment.target.root,
        kind=assignment.target.kind,
    )
    return read_metadata_batch([item], settings.tools.exiftool).get(item.path.resolve())


def _restore_file_times(path: Path, stat_result: os.stat_result) -> None:
    """Keep a metadata-only repair from changing filesystem timestamps."""
    os.utime(path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns))
    if os.name == "nt":
        try:
            _set_windows_creation_time(path, stat_result.st_ctime)
        except OSError:
            LOGGER.warning("Could not restore Windows creation time for %s", path, exc_info=True)


def _write_manifest(root: Path, report: GpsRepairReport, entries: list[GpsRepairEntryResult]) -> Path:
    path = root / "_VacationMediaProcessor_Manifest" / f"{report.run_id}_gps_repair.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": report.run_id,
        "root": root,
        "cancelled": report.cancelled,
        "files": [
            {
                "path": entry.assignment.target.path,
                "method": entry.assignment.method,
                "anchors": entry.assignment.anchor_paths,
                "latitude": entry.assignment.lat,
                "longitude": entry.assignment.lon,
                "backup_path": entry.backup_path,
                "success": entry.success,
                "skipped": entry.skipped,
                "error": entry.error,
            }
            for entry in entries
        ],
    }
    path.write_text(json.dumps(payload, default=json_default, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _emit(callback: ProgressCallback | None, current: int, total: int, message: str) -> None:
    if callback is not None:
        callback(PipelineProgress(Phase.GPS_REPAIR, current, total, message))


def apply_gps_assignments(
    assignments: list[GpsAssignment],
    settings: AppSettings,
    *,
    create_backups: bool,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> GpsRepairReport:
    """Write confirmed coordinates independently, continuing after per-file errors."""
    report = GpsRepairReport(run_id=make_run_id())
    total = len(assignments)
    for index, assignment in enumerate(assignments, start=1):
        if cancel_callback is not None and cancel_callback():
            report.cancelled = True
            break
        path = assignment.target.path
        entry = GpsRepairEntryResult(assignment=assignment)
        report.entries.append(entry)
        _emit(progress_callback, index - 1, total, f"GPS: {path.name}")
        try:
            if not is_valid_position(assignment.lat, assignment.lon):
                raise RuntimeError(
                    f"Ungültige Zielkoordinate {assignment.lat}, {assignment.lon}; "
                    "erwartet werden -90..90 und -180..180."
                )
            before = _read_one(assignment, settings)
            if before is None:
                raise RuntimeError("ExifTool konnte die Datei vor dem Schreiben nicht lesen.")
            if gps_coordinates(before.tags) is not None:
                entry.skipped = True
                entry.readback = before
                continue
            stat_result = path.stat()
            if create_backups:
                destination = backup_dir(assignment.target.root, report.run_id) / _relative_or_name(
                    path, assignment.target.root
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise RuntimeError(f"Backup-Ziel existiert bereits: {destination}")
                shutil.copy2(path, destination)
                entry.backup_path = destination

            args = [settings.tools.exiftool, "-m", "-P", "-overwrite_original_in_place"]
            args.extend(gps_write_arguments(path, assignment.lat, assignment.lon))
            args.append(str(path))
            _require_exiftool_update(run_process(args).stdout, path)
            _restore_file_times(path, stat_result)
            readback = _read_one(assignment, settings)
            coords = gps_coordinates(readback.tags) if readback is not None else None
            if coords is None or haversine_km(coords, (assignment.lat, assignment.lon)) * 1000 > READBACK_TOLERANCE_METRES:
                raise RuntimeError("GPS-Readback stimmt nicht mit der gewünschten Position überein.")
            entry.readback = readback
            entry.success = True
        except Exception as exc:
            LOGGER.exception("GPS repair failed for %s", path)
            entry.error = str(exc)
            if entry.backup_path is not None and entry.backup_path.exists():
                try:
                    shutil.copy2(entry.backup_path, path)
                except OSError as restore_exc:
                    entry.error += f"; Wiederherstellung fehlgeschlagen: {restore_exc}"
        finally:
            _emit(progress_callback, index, total, f"GPS: {path.name}")

    by_root: dict[Path, list[GpsRepairEntryResult]] = defaultdict(list)
    for entry in report.entries:
        by_root[entry.assignment.target.root].append(entry)
    for root, entries in by_root.items():
        try:
            report.manifest_paths.append(_write_manifest(root, report, entries))
        except OSError as exc:
            LOGGER.exception("Could not write GPS repair manifest under %s", root)
            if entries:
                entries[-1].error = "; ".join(part for part in (entries[-1].error, f"Manifest: {exc}") if part)
    return report


__all__ = ["READBACK_TOLERANCE_METRES", "apply_gps_assignments", "gps_write_arguments"]
