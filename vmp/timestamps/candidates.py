"""Timestamp candidate extraction: tag classification and candidate building."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from ..core.models import MediaKind, RawMetadata
from .parsing import (
    get_first_str,
    parse_exif_datetime,
    parse_exif_datetime_with_offset,
    parse_filename_date_only,
    parse_filename_datetime,
    parse_gps_datetime,
    parse_iso_datetime,
    parse_offset,
)


class TimeRole(str, Enum):
    """Semantic role of a timestamp candidate."""

    LOCAL = "local"
    UTC = "utc"
    OFFSET = "offset"
    DATE_ONLY = "date_only"



@dataclass(slots=True)
class TimeCandidate:
    """Candidate datetime or offset extracted from metadata."""

    label: str
    role: TimeRole
    dt: datetime | None
    offset: timedelta | None
    source_tags: tuple[str, ...]
    score: int
    is_embedded_offset: bool = False



@dataclass(slots=True)
class ResolutionEvidence:
    """Evidence gathered while resolving timestamps."""

    matched_tags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)



DATETIME_KEY_HINT_RE = re.compile(r"(date|time|timestamp|creation)", re.IGNORECASE)

OFFSET_KEY_HINT_RE = re.compile(
    r"(utc[_:\-. ]?offset|offsettime|timezone|time[_:\-. ]?zone|tzoffset)",
    re.IGNORECASE,
)


LOCAL_EXIF_KEYS = (
    "Composite:SubSecDateTimeOriginal",
    "Composite:SubSecCreateDate",
    "EXIF:DateTimeOriginal",
    "ExifIFD:DateTimeOriginal",
    "EXIF:CreateDate",
    "ExifIFD:CreateDate",
    "IFD0:ModifyDate",
    "Keys:CreationDate",
    "ItemList:ContentCreateDate",
    "UserData:DateTimeOriginal",
    "QuickTime:CreationDate",
)

OFFSET_KEYS = (
    "EXIF:OffsetTimeOriginal",
    "ExifIFD:OffsetTimeOriginal",
    "EXIF:OffsetTime",
    "ExifIFD:OffsetTime",
    "EXIF:OffsetTimeDigitized",
    "ExifIFD:OffsetTimeDigitized",
    "Canon:TimeZone",
    "Keys:SamsungAndroidUtcOffset",
    "QuickTime:SamsungAndroidUtcOffset",
    "Samsung:SamsungAndroidUtcOffset",
)

UTC_KEYS = (
    "QuickTime:CreateDate",
    "QuickTime:ModifyDate",
    "QuickTime:TrackCreateDate",
    "QuickTime:TrackModifyDate",
    "QuickTime:MediaCreateDate",
    "QuickTime:MediaModifyDate",
    "Track1:TrackCreateDate",
    "Track1:TrackModifyDate",
    "Track2:TrackCreateDate",
    "Track2:TrackModifyDate",
    "Keys:CreationTime",
)



def is_datetime_like_key(key: str) -> bool:
    """Return True if a metadata key likely contains date/time information."""
    return DATETIME_KEY_HINT_RE.search(key) is not None



def is_ignored_datetime_source(key: str) -> bool:
    """Return True for derived datetime tags VMP must not trust."""
    group, _, name = key.partition(":")
    normalized_name = (name or group).casefold()
    normalized_group = group.casefold()
    if normalized_name == "oldestdatetime":
        return True
    return normalized_name == "profiledatetime" and normalized_group.startswith("icc")



def is_core_metadata_datetime_key(key: str) -> bool:
    """Return True for datetime keys that are not filesystem-only fields."""
    if not is_datetime_like_key(key):
        return False
    lowered = key.lower()
    return not (lowered.startswith("file:") or lowered.startswith("system:"))



def is_utc_biased_key(key: str) -> bool:
    """Return True if a key usually stores UTC container timestamps."""
    lowered = key.lower()
    if key in UTC_KEYS:
        return True
    utc_tokens = (
        "utc",
        "encodeddate",
        "taggeddate",
        "creation_time",
        "quicktime:createdate",
        "quicktime:modifydate",
        "quicktime:trackcreatedate",
        "quicktime:trackmodifydate",
        "quicktime:mediacreatedate",
        "quicktime:mediamodifydate",
    )
    return any(token in lowered for token in utc_tokens)



def is_offset_like_key(key: str) -> bool:
    """Return True if a key likely stores an offset."""
    return OFFSET_KEY_HINT_RE.search(key) is not None



def merge_duplicate_candidates(candidates: list[TimeCandidate]) -> list[TimeCandidate]:
    """Merge duplicate candidates while retaining evidence tags."""
    merged: dict[tuple[TimeRole, datetime | None, int | None], TimeCandidate] = {}
    for candidate in candidates:
        offset_seconds = int(candidate.offset.total_seconds()) if candidate.offset is not None else None
        key = (candidate.role, candidate.dt, offset_seconds)
        existing = merged.get(key)
        if existing is None:
            merged[key] = candidate
            continue
        existing.score += 1
        existing.source_tags = tuple(dict.fromkeys((*existing.source_tags, *candidate.source_tags)))
        existing.is_embedded_offset = existing.is_embedded_offset or candidate.is_embedded_offset
    return list(merged.values())



def collect_datetime_value_pool(
    tags: dict[str, Any],
) -> tuple[dict[datetime, int], dict[datetime, set[str]], dict[tuple[datetime, datetime, int], set[str]]]:
    """Parse all datetime-like fields into value and local/UTC/offset pools."""
    value_counts: dict[datetime, int] = {}
    value_sources: dict[datetime, set[str]] = {}
    pair_sources: dict[tuple[datetime, datetime, int], set[str]] = {}
    for key in sorted(tags.keys()):
        if is_ignored_datetime_source(key) or not is_core_metadata_datetime_key(key):
            continue
        value = str(tags.get(key, "")).strip()
        if not value:
            continue
        parsed_iso = parse_iso_datetime(value)
        parsed_exif_tz = parse_exif_datetime_with_offset(value)
        parsed_exif = parse_exif_datetime(value)
        if parsed_iso is not None:
            dt, embedded_offset = parsed_iso
        elif parsed_exif_tz is not None:
            dt, embedded_offset = parsed_exif_tz
        elif parsed_exif is not None:
            dt, embedded_offset = parsed_exif, None
        else:
            continue
        if embedded_offset is not None:
            utc_dt = dt - embedded_offset
            offset_seconds = int(embedded_offset.total_seconds())
            pair_key = (dt, utc_dt, offset_seconds)
            pair_sources.setdefault(pair_key, set()).add(key)
            for value_dt in (dt, utc_dt):
                value_counts[value_dt] = value_counts.get(value_dt, 0) + 1
                value_sources.setdefault(value_dt, set()).add(key)
        else:
            value_counts[dt] = value_counts.get(dt, 0) + 1
            value_sources.setdefault(dt, set()).add(key)
    return value_counts, value_sources, pair_sources



def build_time_candidates(metadata: RawMetadata, kind: MediaKind) -> list[TimeCandidate]:
    """Build normalized time candidates from raw metadata."""
    tags = metadata.tags
    candidates: list[TimeCandidate] = []
    for key in sorted(tags.keys()):
        if is_ignored_datetime_source(key) or not is_core_metadata_datetime_key(key):
            continue
        value = str(tags.get(key, "")).strip()
        if not value:
            continue
        parsed_iso = parse_iso_datetime(value)
        parsed_exif_tz = parse_exif_datetime_with_offset(value)
        parsed_exif = parse_exif_datetime(value)
        if parsed_iso is not None or parsed_exif_tz is not None:
            dt, embedded_offset = parsed_iso if parsed_iso is not None else parsed_exif_tz  # type: ignore[misc]
            if embedded_offset is None:
                if is_utc_biased_key(key):
                    candidates.append(TimeCandidate(key, TimeRole.UTC, dt, timedelta(0), (key,), 88))
                else:
                    local_score = 95 if key in LOCAL_EXIF_KEYS else 75
                    candidates.append(TimeCandidate(key, TimeRole.LOCAL, dt, None, (key,), local_score))
                continue
            local_score = 95 if key in LOCAL_EXIF_KEYS else 84
            utc_score = 92 if is_utc_biased_key(key) else 82
            if not (is_utc_biased_key(key) and embedded_offset == timedelta(0)):
                candidates.append(TimeCandidate(key, TimeRole.LOCAL, dt, embedded_offset, (key,), local_score, True))
                candidates.append(TimeCandidate(key, TimeRole.OFFSET, None, embedded_offset, (key,), 86, True))
            candidates.append(TimeCandidate(key, TimeRole.UTC, dt - embedded_offset, embedded_offset, (key,), utc_score, True))
            continue
        if parsed_exif is not None:
            if is_utc_biased_key(key):
                candidates.append(TimeCandidate(key, TimeRole.UTC, parsed_exif, timedelta(0), (key,), 82))
            else:
                score = 98 if "SubSec" in key else 90 if key in LOCAL_EXIF_KEYS else 72
                candidates.append(TimeCandidate(key, TimeRole.LOCAL, parsed_exif, None, (key,), score))
    for key in OFFSET_KEYS:
        value = get_first_str(tags, key)
        if not value:
            continue
        offset = parse_offset(value)
        if offset is not None:
            candidates.append(TimeCandidate(key, TimeRole.OFFSET, None, offset, (key,), 86))
    for key in sorted(tags.keys()):
        if key in OFFSET_KEYS or not is_offset_like_key(key):
            continue
        value = get_first_str(tags, key)
        offset = parse_offset(value) if value else None
        if offset is not None:
            candidates.append(TimeCandidate(key, TimeRole.OFFSET, None, offset, (key,), 84))
    gps_date = get_first_str(tags, "GPS:GPSDateStamp", "GPS:GPSDate")
    gps_time = get_first_str(tags, "GPS:GPSTimeStamp")
    if gps_date and gps_time:
        gps_dt = parse_gps_datetime(gps_date, gps_time)
        if gps_dt is not None:
            candidates.append(
                TimeCandidate(
                    "GPS:GPSDateStamp+GPS:GPSTimeStamp",
                    TimeRole.UTC,
                    gps_dt,
                    timedelta(0),
                    ("GPS:GPSDateStamp", "GPS:GPSTimeStamp"),
                    65,
                )
            )
    filename_dt = parse_filename_datetime(Path(metadata.source_file).name)
    if filename_dt is not None:
        filename_score = 98 if kind == MediaKind.VIDEO else 70
        candidates.append(TimeCandidate("filename_datetime", TimeRole.LOCAL, filename_dt, None, ("System:FileName",), filename_score))
    else:
        filename_date = parse_filename_date_only(Path(metadata.source_file).name)
        if filename_date is not None:
            candidates.append(TimeCandidate("filename_date_only", TimeRole.DATE_ONLY, filename_date, None, ("System:FileName",), 40))
    candidates = merge_duplicate_candidates(candidates)
    if kind == MediaKind.IMAGE:
        candidates.sort(key=lambda item: (item.role != TimeRole.LOCAL, -item.score, item.label))
    else:
        candidates.sort(key=lambda item: (item.role == TimeRole.DATE_ONLY, -item.score, item.label))
    return candidates



def choose_best_local(candidates: Sequence[TimeCandidate], kind: MediaKind) -> TimeCandidate | None:
    """Select the best local datetime candidate."""
    local_candidates = [c for c in candidates if c.role == TimeRole.LOCAL and c.dt is not None]
    if kind == MediaKind.VIDEO:
        embedded = [c for c in local_candidates if c.offset is not None or c.is_embedded_offset]
        if embedded:
            return max(embedded, key=lambda item: item.score)
    return max(local_candidates, key=lambda item: item.score) if local_candidates else None



def choose_best_offset(candidates: Sequence[TimeCandidate]) -> TimeCandidate | None:
    """Select the best offset candidate."""
    offsets = [c for c in candidates if c.role == TimeRole.OFFSET and c.offset is not None]
    return max(offsets, key=lambda item: item.score) if offsets else None



def choose_best_utc(candidates: Sequence[TimeCandidate]) -> TimeCandidate | None:
    """Select the best UTC candidate."""
    utc_candidates = [c for c in candidates if c.role == TimeRole.UTC and c.dt is not None]
    return max(utc_candidates, key=lambda item: item.score) if utc_candidates else None



def choose_date_only(candidates: Sequence[TimeCandidate]) -> TimeCandidate | None:
    """Select a date-only candidate."""
    date_only = [c for c in candidates if c.role == TimeRole.DATE_ONLY and c.dt is not None]
    return max(date_only, key=lambda item: item.score) if date_only else None


