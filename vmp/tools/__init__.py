"""ExifTool metadata writing plus facade for the image/video tool wrappers.

The FFmpeg/FFprobe wrappers live in :mod:`vmp.tools.video`
and the NConvert/pillow-heif wrappers in
:mod:`vmp.tools.image`; both are re-exported here as
the package's public API.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path

from .image import (  # noqa: F401
    _output_needs_orientation_fix,
    convert_image,
    embed_gdepth,
    maintain_jpeg,
)
from ..core.logging_config import get_logger
from ..metadata import metadata_write_tags
from ..core.models import AnalysisResult, AppSettings, MediaKind
from ..core.processes import run_process
from .video import (  # noqa: F401
    MP4_COPY_SAFE_AUDIO_CODECS,
    _audio_transcode_args,
    probe_video,
    transcode_video,
)

LOGGER = get_logger(__name__)

def write_metadata(result: AnalysisResult, path: Path, settings: AppSettings) -> None:
    """Apply metadata normalization and cleanup to a media file."""
    set_tags, delete_tags = metadata_write_tags(result, settings.metadata)
    should_set_filesystem_dates = _should_set_filesystem_dates(result, settings)
    if not set_tags and not delete_tags and not should_set_filesystem_dates:
        LOGGER.info("No metadata writes needed for %s", path)
        return
    if set_tags or delete_tags:
        LOGGER.info("Writing metadata path=%s set_tags=%s delete_tags=%s", path, sorted(set_tags), sorted(delete_tags))
        args = [settings.tools.exiftool, "-m", "-overwrite_original_in_place"]
        args.extend(f"-{tag}={value}" for tag, value in set_tags.items())
        args.extend(f"-{tag}=" for tag in delete_tags)
        args.append(str(path))
        run_process(args)
    if should_set_filesystem_dates:
        _set_filesystem_dates(path, result.resolved.local_dt)


def _should_set_filesystem_dates(result: AnalysisResult, settings: AppSettings) -> bool:
    """Return whether filesystem timestamps should be applied from local capture time."""
    return (
        settings.metadata.set_filesystem_dates
        and result.resolved.local_dt is not None
        and not result.resolved.local_date_only
    )


def _set_filesystem_dates(path: Path, local_dt: datetime | None) -> None:
    """Set filesystem access/modify dates and creation date where the OS allows it."""
    if local_dt is None:
        return
    timestamp = _local_wall_time_timestamp(local_dt)
    LOGGER.info("Setting filesystem timestamps path=%s local=%s timestamp=%s", path, local_dt, timestamp)
    os.utime(path, (timestamp, timestamp))
    if os.name == "nt":
        # Best-effort: the metadata write already succeeded at this point, so a
        # locked file / AV scan must not fail the whole item over the creation time.
        try:
            _set_windows_creation_time(path, timestamp)
        except OSError:
            LOGGER.warning("Could not set Windows creation time for %s", path, exc_info=True)


def _local_wall_time_timestamp(value: datetime) -> float:
    """Interpret capture local time as the desired wall-clock filesystem time."""
    local_wall_time = value.replace(tzinfo=None)
    return local_wall_time.timestamp()


def _set_windows_creation_time(path: Path, timestamp: float) -> None:
    """Set the Windows creation time; raises OSError when the file cannot be opened."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Without an explicit restype, ctypes truncates the HANDLE to a signed 32-bit
    # int on 64-bit Python, so the INVALID_HANDLE_VALUE comparison below would
    # never match and failures would go undetected.
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.SetFileTime.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    ticks = int((timestamp + 11644473600) * 10000000)
    filetime = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
    invalid_handle = wintypes.HANDLE(-1).value
    handle = kernel32.CreateFileW(
        str(path),
        0x0100,  # FILE_WRITE_ATTRIBUTES
        0x00000001 | 0x00000002 | 0x00000004,  # FILE_SHARE_READ | WRITE | DELETE
        None,
        3,  # OPEN_EXISTING
        0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
        None,
    )
    if handle == invalid_handle or handle is None:
        raise OSError(f"Could not open file handle for creation time (error {ctypes.get_last_error()}): {path}")
    try:
        if not kernel32.SetFileTime(handle, ctypes.byref(filetime), None, ctypes.byref(filetime)):
            raise OSError(f"Could not set Windows creation time (error {ctypes.get_last_error()}): {path}")
    finally:
        kernel32.CloseHandle(handle)


def copy_all_metadata(source: Path, target: Path, settings: AppSettings, kind: MediaKind) -> None:
    """Copy all metadata from source to target using ExifTool.

    For images, the Orientation tag is stripped after the copy rather than forced to
    ``1``: ``convert_image`` already baked the EXIF orientation into the pixels (for
    HEIC/HEIF via libheif, verified/repaired against the true display size), so keeping
    any Orientation value would make viewers rotate the already-oriented output a second
    time. Deleting the tag is
    deterministic across IFD0 and XMP-tiff; setting it to ``1`` only touches IFD0 and
    is unreliable via ExifTool's printconv lookup.
    """
    LOGGER.info("Copying all metadata source=%s target=%s kind=%s", source, target, kind.value)
    args = [
        settings.tools.exiftool,
        "-m",
        "-overwrite_original_in_place",
        "-TagsFromFile",
        str(source),
        "-all:all",
        "-unsafe",
    ]
    if kind == MediaKind.IMAGE:
        args.extend(["-IFD0:Orientation=", "-XMP-tiff:Orientation="])
    args.append(str(target))
    run_process(args)

