"""Run manifest serialization."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from .core.models import AppSettings, MediaPlan, PipelineReport, RawMetadata


def json_default(value: Any) -> Any:
    """Serialize common manifest values."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Cannot serialize {type(value)!r}")


def write_manifest(
    path: Path,
    *,
    root: Path,
    settings: AppSettings,
    plans: list[MediaPlan],
    report: PipelineReport | None,
) -> None:
    """Write a JSON manifest for a dry-run or apply run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "root": root,
        "settings": settings,
        "plans": plans,
        "report": report,
    }
    path.write_text(json.dumps(payload, default=json_default, indent=2, sort_keys=True), encoding="utf-8")


def _manifest_key(root: Path, before: Path) -> str:
    """Return a stable key based on the original file path."""
    try:
        return str(before.relative_to(root))
    except ValueError:
        return str(before)


def _metadata_lookup(records: dict[Path, RawMetadata] | None, path: Path) -> RawMetadata | None:
    """Return metadata for a path from ExifTool's resolved-path records."""
    if records is None:
        return None
    return records.get(path.resolve())


def _write_metadata_snapshot(
    path: Path,
    *,
    root: Path,
    settings: AppSettings,
    plans: list[MediaPlan],
    report: PipelineReport | None,
    snapshot: str,
    after_metadata: dict[Path, RawMetadata] | None,
) -> None:
    """Write one side of a before/after metadata comparison manifest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    file_entries: dict[str, Any] = {}
    for plan in plans:
        before_path = plan.analysis.item.path
        after_path = plan.final_path or before_path
        key = _manifest_key(root, before_path)
        file_path = before_path if snapshot == "before" else after_path
        metadata = plan.analysis.metadata if snapshot == "before" else _metadata_lookup(after_metadata, after_path)
        entry: dict[str, Any] = {
            "snapshot": snapshot,
            "original_path": before_path,
            "file_path": file_path,
            "final_path": after_path,
            "metadata": metadata,
        }
        file_entries[key] = entry
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "root": root,
        "settings": settings,
        "report": report,
        "files": file_entries,
    }
    path.write_text(json.dumps(payload, default=json_default, indent=2, sort_keys=True), encoding="utf-8")


def write_before_after_manifests(
    before_path: Path,
    after_path: Path,
    *,
    root: Path,
    settings: AppSettings,
    plans: list[MediaPlan],
    report: PipelineReport | None,
    after_metadata: dict[Path, RawMetadata] | None = None,
) -> None:
    """Write comparable before/after manifests keyed by original filenames."""
    _write_metadata_snapshot(
        before_path,
        root=root,
        settings=settings,
        plans=plans,
        report=report,
        snapshot="before",
        after_metadata=after_metadata,
    )
    _write_metadata_snapshot(
        after_path,
        root=root,
        settings=settings,
        plans=plans,
        report=report,
        snapshot="after",
        after_metadata=after_metadata,
    )
