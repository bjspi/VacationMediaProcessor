"""Media file discovery."""

from __future__ import annotations

import os
from pathlib import Path

from .models import (
    IMAGE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    VIDEO_EXTENSIONS,
    MediaItem,
    MediaKind,
)

IGNORED_DIR_NAMES: frozenset[str] = frozenset(
    {
        "_VacationMediaProcessor_Work",
        "_VacationMediaProcessor_Backup",
        "_VacationMediaProcessor_Manifest",
        "_VacationMediaProcessor_Temp",
    }
)


def normalize_root(path: Path) -> Path:
    """Resolve and validate a user-selected media root."""
    root = path.expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Folder does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Path is not a folder: {root}")
    return root


def classify_media(path: Path) -> MediaKind | None:
    """Return the media kind for a supported path."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return MediaKind.IMAGE
    if suffix in VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    return None


def _is_ignored_dir(name: str) -> bool:
    """Return True for generated/backup and hidden directories."""
    return name in IGNORED_DIR_NAMES or name.startswith(".")


def discover_media(root: Path, recursive: bool = True) -> list[MediaItem]:
    """Discover supported media files below a root folder.

    Uses ``os.walk`` with in-place pruning so the (potentially huge) backup/work
    trees and hidden directories are never descended into, and filters on the
    cheap suffix check before touching the filesystem again.
    """
    normalized_root = normalize_root(root)
    items: list[MediaItem] = []

    def _append_candidates(directory: str, filenames: list[str]) -> None:
        for filename in filenames:
            path = Path(directory) / filename
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            kind = classify_media(path)
            if kind is None:
                continue
            items.append(MediaItem(path=path, root=normalized_root, kind=kind))

    if recursive:
        for dirpath, dirnames, filenames in os.walk(normalized_root):
            dirnames[:] = [name for name in dirnames if not _is_ignored_dir(name)]
            _append_candidates(dirpath, filenames)
    else:
        with os.scandir(normalized_root) as entries:
            filenames = [entry.name for entry in entries if entry.is_file()]
        _append_candidates(str(normalized_root), filenames)
    return sorted(items, key=lambda item: str(item.relative_path).lower())
