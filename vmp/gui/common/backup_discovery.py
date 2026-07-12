"""Locate backup copies of processed media via run manifests and backup trees.

Qt-free helpers used by the main window's diff actions: when the in-memory
backup map is empty (e.g. after an app restart), the newest matching backup is
rediscovered from the ``_VacationMediaProcessor_Manifest`` JSON files and the
``_VacationMediaProcessor_Backup`` run directories.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ...core.discovery import normalize_root
from .file_transfer import same_path
from ...core.models import MediaPlan

LOGGER = logging.getLogger("vmp.gui.common.backup_discovery")


def _mtime_or_zero(path: Path) -> float:
    """Sort key that tolerates files vanishing between glob and stat."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def path_from_json(value: object) -> Path | None:
    """Return a Path from a JSON string value."""
    if not isinstance(value, str) or not value:
        return None
    return Path(value)


def original_paths_from_manifest_payload(payload: object, current: Path) -> list[Path]:
    """Extract original paths matching the current path from one manifest payload."""
    if not isinstance(payload, dict):
        return []
    originals: list[Path] = []
    files = payload.get("files")
    if isinstance(files, dict):
        for entry in files.values():
            if not isinstance(entry, dict):
                continue
            file_path = path_from_json(entry.get("file_path"))
            final_path = path_from_json(entry.get("final_path"))
            original_path = path_from_json(entry.get("original_path"))
            if original_path is not None and (
                same_path(file_path, current) or same_path(final_path, current)
            ):
                originals.append(original_path)
    plans = payload.get("plans")
    if isinstance(plans, list):
        for plan_payload in plans:
            if not isinstance(plan_payload, dict):
                continue
            final_path = path_from_json(plan_payload.get("final_path"))
            analysis = plan_payload.get("analysis")
            item = analysis.get("item") if isinstance(analysis, dict) else None
            original_path = path_from_json(item.get("path")) if isinstance(item, dict) else None
            if original_path is not None and (
                same_path(final_path, current) or same_path(original_path, current)
            ):
                originals.append(original_path)
    return originals


def original_paths_from_manifests(root: Path, current: Path) -> list[Path]:
    """Return original source paths for a current/final path from recent manifests."""
    manifest_dir = root / "_VacationMediaProcessor_Manifest"
    if not manifest_dir.exists():
        return []
    originals: list[Path] = []
    manifests = sorted(manifest_dir.glob("*.json"), key=_mtime_or_zero, reverse=True)
    for manifest in manifests:
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for original in original_paths_from_manifest_payload(payload, current):
            if original not in originals:
                originals.append(original)
    return originals


def existing_backup_variant(path: Path) -> Path | None:
    """Return an exact backup path or a collision-suffixed variant."""
    if path.exists():
        return path
    if not path.parent.exists():
        return None
    candidates = sorted(
        path.parent.glob(f"{path.stem}-*{path.suffix}"),
        key=_mtime_or_zero,
        reverse=True,
    )
    return candidates[0] if candidates else None


def latest_existing_backup_for_original(root: Path, backup_root: Path, original: Path) -> Path | None:
    """Return the newest backup file for an original path."""
    try:
        relative = original.relative_to(root)
    except ValueError:
        return None
    run_dirs = [path for path in backup_root.iterdir() if path.is_dir()]
    for run_dir in sorted(run_dirs, key=lambda path: path.name, reverse=True):
        backup = existing_backup_variant(run_dir / relative)
        if backup is not None:
            return backup
    return None


def discover_backup_path_for_plan(plan: MediaPlan) -> Path | None:
    """Find a backup path from manifests or backup folders when the in-memory map is empty."""
    current = plan.analysis.item.path.resolve()
    root = normalize_root(plan.analysis.item.root)
    backup_root = root / "_VacationMediaProcessor_Backup"
    if not backup_root.exists():
        return None
    original_paths = original_paths_from_manifests(root, current)
    if not original_paths:
        original_paths = [plan.analysis.item.path]
    for original in original_paths:
        backup = latest_existing_backup_for_original(root, backup_root, original)
        if backup is not None:
            LOGGER.info("Discovered backup for diff current=%s backup=%s", current, backup)
            return backup
    return None
