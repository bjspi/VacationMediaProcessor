"""Qt-free file transfer helpers shared by the Trip Lasso and pair cleanup dialogs."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..lasso.trip_selection import TripRecord

LOGGER = logging.getLogger("vmp.gui.common.file_transfer")

_ILLEGAL_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_folder_name(name: str) -> str:
    """Make a user- or geocoder-supplied folder name safe as a single path segment.

    Real place names such as "Biel/Bienne" contain path separators, which would
    silently create nested directories (or fail on ``:``/``?``). Illegal
    characters collapse to a hyphen; surrounding whitespace/dots are trimmed.
    """
    cleaned = _ILLEGAL_NAME_CHARS.sub("-", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned


def unique_target(dest_dir: Path, name: str) -> Path:
    """Return a non-colliding target path inside ``dest_dir`` for ``name``."""
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    index = 1
    while True:
        candidate = dest_dir / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def perform_transfer(sources: list[Path], dest_dir: Path, copy: bool) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Move or copy ``sources`` into ``dest_dir``; return (succeeded, errors)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    errors: list[tuple[Path, str]] = []
    for source in sources:
        try:
            target = unique_target(dest_dir, source.name)
            if copy:
                shutil.copy2(str(source), str(target))
            else:
                shutil.move(str(source), str(target))
            moved.append(source)
            LOGGER.info("%s %s -> %s", "Copied" if copy else "Moved", source, target)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Transfer failed for %s: %s", source, exc)
            errors.append((source, str(exc)))
    return moved, errors


def parent_directory(path: Path) -> Path:
    """Return the parent folder, keeping roots stable."""
    parent = path.parent
    return path if parent == path else parent


def path_key(path: Path) -> str:
    """Return a stable path key for Lasso session comparisons."""
    try:
        return str(path.resolve()).casefold()
    except OSError:
        return str(path).casefold()


def same_path(left: Path | None, right: Path | None) -> bool:
    """Compare paths with Windows-friendly normalization."""
    if left is None or right is None:
        return False
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return str(left).casefold() == str(right).casefold()


def remaining_records(records: "list[TripRecord]", removed: list[Path]) -> "list[TripRecord]":
    """Return records excluding removed paths."""
    removed_keys = {path_key(path) for path in removed}
    return [record for record in records if path_key(record.path) not in removed_keys]


def open_in_default_app(path: Path) -> None:
    """Open a file in the OS default application."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not open %s in default app", path, exc_info=True)
