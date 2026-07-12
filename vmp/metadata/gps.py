"""GPS coordinate extraction from ExifTool tag dictionaries."""

from __future__ import annotations

import re
from typing import Any

from ..timestamps.parsing import get_first_str


_GPS_DMS_RE = re.compile(
    r"(?P<deg>[-+]?\d+(?:\.\d+)?)\s*(?:deg|°)?"
    r"(?:\s*(?P<min>\d+(?:\.\d+)?)\s*')?"
    r"(?:\s*(?P<sec>\d+(?:\.\d+)?)\s*\")?"
    r"\s*(?P<ref>[NSEW])?",
    re.IGNORECASE,
)



def _parse_coordinate(value: str | None) -> float | None:
    """Parse a single GPS coordinate (decimal or 'D deg M' S" H') into a float."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    match = _GPS_DMS_RE.search(value)
    if match is None:
        return None
    degrees = float(match.group("deg"))
    minutes = float(match.group("min") or 0.0)
    seconds = float(match.group("sec") or 0.0)
    decimal = abs(degrees) + minutes / 60.0 + seconds / 3600.0
    ref = (match.group("ref") or "").upper()
    negative = degrees < 0 or ref in ("S", "W")
    return -decimal if negative else decimal



def gps_coordinates(tags: dict[str, Any]) -> tuple[float, float] | None:
    """Return decimal (latitude, longitude) from ExifTool tags, or None.

    Handles both the formatted DMS strings ExifTool emits by default and plain
    decimal values, drawing from the GPS, Composite, and XMP groups.
    """
    lat_raw = get_first_str(tags, "GPS:GPSLatitude", "Composite:GPSLatitude", "XMP:GPSLatitude")
    lon_raw = get_first_str(tags, "GPS:GPSLongitude", "Composite:GPSLongitude", "XMP:GPSLongitude")
    if lat_raw is None or lon_raw is None:
        position = get_first_str(tags, "Composite:GPSPosition")
        if position and "," in position:
            lat_part, lon_part = position.split(",", 1)
            lat_raw = lat_raw or lat_part
            lon_raw = lon_raw or lon_part
    if lat_raw is None or lon_raw is None:
        return None
    lat_ref = get_first_str(tags, "GPS:GPSLatitudeRef", "Composite:GPSLatitudeRef")
    lon_ref = get_first_str(tags, "GPS:GPSLongitudeRef", "Composite:GPSLongitudeRef")
    if lat_ref and not re.search(r"[NSEW]", lat_raw, re.IGNORECASE):
        lat_raw = f"{lat_raw} {lat_ref}"
    if lon_ref and not re.search(r"[NSEW]", lon_raw, re.IGNORECASE):
        lon_raw = f"{lon_raw} {lon_ref}"
    latitude = _parse_coordinate(lat_raw)
    longitude = _parse_coordinate(lon_raw)
    if latitude is None or longitude is None:
        return None
    if latitude == 0.0 and longitude == 0.0:
        return None
    if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
        return None
    return (latitude, longitude)



def has_gps(tags: dict[str, Any]) -> bool:
    """Return True when the tags contain usable GPS coordinates."""
    return gps_coordinates(tags) is not None


