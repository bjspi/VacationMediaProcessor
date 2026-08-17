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

_ISO6709_RE = re.compile(
    r"^\s*(?P<lat>[+-]\d{2}(?:\.\d+)?)"
    r"(?P<lon>[+-]\d{3}(?:\.\d+)?)"
    r"(?:[+-]\d+(?:\.\d+)?)?/?\s*$"
)


def _parse_position(value: str | None) -> tuple[float, float] | None:
    """Parse a combined decimal/ISO-6709 latitude-longitude value."""
    if value is None:
        return None
    match = _ISO6709_RE.match(value)
    if match is not None:
        return (float(match.group("lat")), float(match.group("lon")))
    if "," in value:
        left, right = value.split(",", 1)
        latitude = _parse_coordinate(left)
        longitude = _parse_coordinate(right)
        if latitude is not None and longitude is not None:
            return (latitude, longitude)
    parts = [part for part in re.split(r"\s*,\s*|\s+", value.strip()) if part]
    if len(parts) >= 2:
        latitude = _parse_coordinate(parts[0])
        longitude = _parse_coordinate(parts[1])
        if latitude is not None and longitude is not None:
            return (latitude, longitude)
    return None



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
    lat_raw = get_first_str(
        tags,
        "GPS:GPSLatitude",
        "Composite:GPSLatitude",
        "XMP:GPSLatitude",
        "XMP-exif:GPSLatitude",
    )
    lon_raw = get_first_str(
        tags,
        "GPS:GPSLongitude",
        "Composite:GPSLongitude",
        "XMP:GPSLongitude",
        "XMP-exif:GPSLongitude",
    )
    if lat_raw is None or lon_raw is None:
        position = get_first_str(tags, "Composite:GPSPosition")
        combined = _parse_position(position)
        if combined is None:
            combined = _parse_position(
                get_first_str(
                    tags,
                    "Keys:GPSCoordinates",
                    "UserData:GPSCoordinates",
                    "ItemList:GPSCoordinates",
                    "QuickTime:GPSCoordinates",
                )
            )
        if combined is not None:
            lat_raw = lat_raw or str(combined[0])
            lon_raw = lon_raw or str(combined[1])
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


