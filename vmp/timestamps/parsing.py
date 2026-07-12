"""Datetime/offset/duration parsing and formatting for ExifTool tag values.

Pure functions without I/O: everything here maps strings (tag values, file
names) to ``datetime``/``timedelta`` values and back. The resolution heuristics
live in :mod:`vmp.timestamps.resolution`.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

EXIF_DT_RE = re.compile(
    r"^(?P<year>\d{4}):(?P<month>\d{2}):(?P<day>\d{2}) "
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<subsec>\d+))?$"
)
EXIF_DT_TZ_RE = re.compile(
    r"^(?P<year>\d{4}):(?P<month>\d{2}):(?P<day>\d{2}) "
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<subsec>\d+))?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2}| ?UTC)$"
)
ISO_DT_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[ T]"
    r"(?P<time>\d{2}:\d{2}:\d{2})(?:\.(?P<subsec>\d+))?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)
OFFSET_RE = re.compile(r"^(?P<sign>[+-])(?P<hours>\d{2}):?(?P<minutes>\d{2})$")

FILENAME_DT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[_-](\d{2})-(\d{2})-(\d{2})(?!\d)"),
    re.compile(r"(?:IMG|VID|PXL|WA)[-_]?(\d{4})(\d{2})(\d{2})[-_]?(\d{2})(\d{2})(\d{2})", re.I),
)
FILENAME_DATE_ONLY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"IMG-(\d{4})(\d{2})(\d{2})-WA\d+", re.IGNORECASE),
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"),
)


def safe_make_datetime(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    microsecond: int = 0,
) -> datetime | None:
    """Create a datetime and return None for invalid values."""
    try:
        return datetime(year, month, day, hour, minute, second, microsecond)
    except ValueError:
        return None


def _datetime_from_groups(groups: dict[str, str | None]) -> datetime | None:
    """Build a datetime from regex group values."""
    microsecond = 0
    if groups.get("subsec"):
        microsecond = int((str(groups["subsec"]) + "000000")[:6])
    return safe_make_datetime(
        int(str(groups["year"])),
        int(str(groups["month"])),
        int(str(groups["day"])),
        int(str(groups["hour"])),
        int(str(groups["minute"])),
        int(str(groups["second"])),
        microsecond,
    )


def parse_exif_datetime(value: str) -> datetime | None:
    """Parse an EXIF datetime without timezone."""
    match = EXIF_DT_RE.match(value.strip())
    return _datetime_from_groups(match.groupdict()) if match else None


def parse_offset(value: str) -> timedelta | None:
    """Parse a timezone offset such as +08:00 or +0800."""
    match = OFFSET_RE.match(value.strip())
    if not match:
        return None
    sign = 1 if match.group("sign") == "+" else -1
    return timedelta(hours=sign * int(match.group("hours")), minutes=sign * int(match.group("minutes")))


def normalize_tz_suffix(value: str) -> str:
    """Normalize UTC suffix variants before parsing."""
    stripped = value.strip()
    if stripped.upper().endswith(" UTC"):
        return stripped[:-4] + "Z"
    if stripped.upper().endswith("UTC"):
        return stripped[:-3] + "Z"
    return stripped


def parse_iso_datetime(value: str) -> tuple[datetime, timedelta | None] | None:
    """Parse an ISO-like datetime and optional offset."""
    match = ISO_DT_RE.match(normalize_tz_suffix(value))
    if not match:
        return None
    subsec = match.group("subsec") or ""
    try:
        dt = datetime.fromisoformat(
            f"{match.group('date')}T{match.group('time')}.{(subsec + '000000')[:6]}"
        )
    except ValueError:
        return None
    tz_part = match.group("tz")
    if tz_part is None:
        return dt, None
    if tz_part == "Z":
        return dt, timedelta(0)
    offset = parse_offset(tz_part)
    return (dt, offset) if offset is not None else None


def parse_exif_datetime_with_offset(value: str) -> tuple[datetime, timedelta] | None:
    """Parse an EXIF datetime with embedded timezone offset."""
    match = EXIF_DT_TZ_RE.match(normalize_tz_suffix(value))
    if not match:
        return None
    dt = _datetime_from_groups(match.groupdict())
    if dt is None:
        return None
    tz_part = match.group("tz")
    if tz_part == "Z":
        return dt, timedelta(0)
    offset = parse_offset(tz_part.strip())
    return (dt, offset) if offset is not None else None


def parse_gps_datetime(date_value: str, time_value: str) -> datetime | None:
    """Parse GPS date and time tags into UTC."""
    date_match = re.match(r"^(\d{4}):(\d{2}):(\d{2})$", date_value.strip())
    if not date_match:
        return None
    numbers = re.findall(r"\d+(?:\.\d+)?", time_value)
    if len(numbers) < 3:
        return None
    seconds_float = float(numbers[2])
    base = safe_make_datetime(
        int(date_match.group(1)),
        int(date_match.group(2)),
        int(date_match.group(3)),
        int(float(numbers[0])),
        int(float(numbers[1])),
        0,
    )
    if base is None:
        return None
    return base + timedelta(seconds=seconds_float)


def parse_filename_datetime(filename: str | Path) -> datetime | None:
    """Extract a datetime from a filename."""
    parsed, _ = parse_filename_datetime_with_format(filename)
    return parsed


def parse_filename_datetime_with_format(filename: str | Path) -> tuple[datetime | None, str | None]:
    """Extract a datetime plus matched filename format hint."""
    name = Path(filename).name
    for pattern in FILENAME_DT_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        parts = [int(value) for value in match.groups()[:6]]
        dt = safe_make_datetime(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5])
        if dt is not None:
            return dt, pattern.pattern
    return None, None


def parse_filename_date_only(filename: str | Path) -> datetime | None:
    """Extract a date-only value from a filename."""
    name = Path(filename).name
    for pattern in FILENAME_DATE_ONLY_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        dt = safe_make_datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if dt is not None:
            return dt
    return None


def format_exif_datetime(value: datetime) -> str:
    """Format a datetime for ExifTool."""
    return value.strftime("%Y:%m:%d %H:%M:%S")


def format_offset(value: timedelta) -> str:
    """Format an offset as +HH:MM."""
    total_minutes = int(value.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def format_offset_compact(value: timedelta) -> str:
    """Format an offset as +HHMM."""
    return format_offset(value).replace(":", "")


def format_iso_with_offset(local_dt: datetime, offset: timedelta) -> str:
    """Format a local datetime with offset."""
    return f"{local_dt.strftime('%Y-%m-%dT%H:%M:%S')}{format_offset(offset)}"


def format_iso_z(utc_dt: datetime) -> str:
    """Format a UTC datetime with a Z suffix."""
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def get_first_str(tags: dict[str, Any], *keys: str) -> str | None:
    """Return the first present tag as a string."""
    for key in keys:
        value = tags.get(key)
        if value is not None:
            return str(value)
    return None


def has_any_tag(tags: dict[str, Any], *keys: str) -> bool:
    """Return True if any tag exists."""
    return any(key in tags for key in keys)


_DURATION_SECONDS_RE = re.compile(r"^\s*([\d.]+)\s*s\s*$", re.IGNORECASE)


def _parse_duration_value(value: str) -> float | None:
    """Parse one ExifTool duration string ('0:00:52', '26.99 s', '52') to seconds."""
    text = str(value).strip()
    if not text:
        return None
    match = _DURATION_SECONDS_RE.match(text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    if ":" in text:
        seconds = 0.0
        try:
            for part in text.split(":"):
                seconds = seconds * 60 + float(part)
            return seconds
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def _nearest_quarter_hour_offset(seconds: float) -> tuple[timedelta, float]:
    """Return the nearest 15-minute timezone offset and the drift from it."""
    nearest = round(seconds / 900.0) * 900.0
    return timedelta(seconds=nearest), abs(seconds - nearest)


def parse_duration_seconds(tags: dict[str, Any]) -> float | None:
    """Return the media duration in seconds from ExifTool duration tags."""
    value = get_first_str(
        tags,
        "QuickTime:Duration",
        "Composite:Duration",
        "Matroska:Duration",
        "Track1:TrackDuration",
        "Track1:MediaDuration",
    )
    if value is None:
        for key in sorted(tags.keys()):
            if key.endswith("Duration"):
                value = get_first_str(tags, key)
                if value:
                    break
    return _parse_duration_value(value) if value else None
