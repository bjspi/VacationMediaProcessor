"""ExifTool write/cleanup tag plans built from a resolved analysis."""

from __future__ import annotations

from typing import Any, Iterable

from ..core.models import (
    AnalysisResult,
    ApplyMode,
    MediaKind,
    MetadataSettings,
    ResolvedTimestamp,
)
from ..timestamps.parsing import (
    format_exif_datetime,
    format_iso_with_offset,
    format_iso_z,
    format_offset,
    format_offset_compact,
    has_any_tag,
)


def build_normalizer_note(resolved: ResolvedTimestamp) -> str:
    """Build a small audit comment describing normalized timestamp evidence."""
    local = resolved.local_dt.isoformat(sep=" ") if resolved.local_dt else "-"
    utc = resolved.utc_dt.isoformat(sep=" ") if resolved.utc_dt else "-"
    offset = format_offset(resolved.offset) if resolved.offset else "-"
    return f"VacationMediaProcessor normalized timestamps; local={local}; utc={utc}; offset={offset}; confidence={resolved.confidence.value}"



def has_existing_normalizer_comment(tags: dict[str, Any], kind: MediaKind) -> bool:
    """Return True when a normalizer comment already exists."""
    keys = ("EXIF:UserComment", "File:Comment") if kind == MediaKind.IMAGE else ("QuickTime:Comment", "Keys:Comment")
    return any("VacationMediaProcessor" in str(tags.get(key, "")) for key in keys)



GROUP_CLEANUP_TAGS: frozenset[str] = frozenset(
    {
        "Trailer:All",
        "trailer:all",
        "XMP:All",
        "XMP-GDepth:All",
        "XMP-GImage:All",
        "MPImage2:All",
    }
)


NON_WRITABLE_CLEANUP_TAGS: frozenset[str] = frozenset(
    {
        "Samsung:Trailer",
        "Samsung:TrailerOffset",
        "Samsung:TrailerLength",
    }
)



def build_cleanup_tags(kind: MediaKind, aggressive: bool = False) -> list[str]:
    """Return conservative cleanup tags."""
    common = [
        "XMP-xmp:CreateDate",
        "XMP-xmp:ModifyDate",
        "XMP-photoshop:DateCreated",
        "GPS:GPSDateStamp",
        "GPS:GPSTimeStamp",
    ]
    if kind == MediaKind.IMAGE:
        return common
    video_tags = [
        *common,
        "Trailer:All",
        "trailer:all",
        "Samsung:Trailer",
        "Samsung:TrailerOffset",
        "Samsung:TrailerLength",
    ]
    if aggressive:
        video_tags.append("XMP:All")
    return video_tags



def build_samsung_trailer_cleanup_tags() -> list[str]:
    """Return Samsung-focused cleanup tags."""
    return [
        "Trailer:All",
        "trailer:all",
        "Samsung:Trailer",
        "Samsung:TrailerOffset",
        "Samsung:TrailerLength",
        "Samsung:EmbeddedVideoType",
        "Samsung:EmbeddedVideoFile",
        "Samsung:EmbeddedVideoSize",
        "Samsung:DepthMapData",
        "Samsung:DepthMapVersion",
        "Samsung:DepthMapNear",
        "Samsung:DepthMapFar",
        "Samsung:DepthMapBlurLevel",
        "XMP-GDepth:All",
        "XMP-GImage:All",
        "MPImage2:All",
    ]



def build_gps_datetime_cleanup_tags() -> list[str]:
    """Return GPS datetime cleanup tags."""
    return ["GPS:GPSDateStamp", "GPS:GPSDate", "GPS:GPSTimeStamp"]



def _writable_cleanup_tags(result: AnalysisResult, candidate_tags: Iterable[str]) -> list[str]:
    """Filter cleanup tags to writable ones that exist on (or group-apply to) the file."""
    available = {key.casefold() for key in result.metadata.tags}
    tags: list[str] = []
    for tag in candidate_tags:
        if tag in NON_WRITABLE_CLEANUP_TAGS:
            continue
        if tag in GROUP_CLEANUP_TAGS or tag.casefold() in available:
            tags.append(tag)
    return tags



def cleanup_tags_for_result(result: AnalysisResult, aggressive: bool = False) -> list[str]:
    """Return cleanup tags that should be safe and useful for this file."""
    return _writable_cleanup_tags(result, build_cleanup_tags(result.item.kind, aggressive))



def samsung_cleanup_tags_for_result(result: AnalysisResult) -> list[str]:
    """Return Samsung cleanup tags without noisy non-writable pseudo tags."""
    return _writable_cleanup_tags(result, build_samsung_trailer_cleanup_tags())



def guess_offset_key(tags: dict[str, Any]) -> str:
    """Guess which MP4 local/offset helper field should be written."""
    if has_any_tag(tags, "Keys:CreationDate"):
        return "Keys:CreationDate"
    if has_any_tag(tags, "Keys:SamsungAndroidUtcOffset", "Samsung:SamsungAndroidUtcOffset"):
        return "Keys:SamsungAndroidUtcOffset"
    return "Keys:CreationDate"



def metadata_write_tags(result: AnalysisResult, settings: MetadataSettings) -> tuple[dict[str, str], list[str]]:
    """Build ExifTool set/delete tags for one analyzed file."""
    mode = settings.apply_mode
    if mode == ApplyMode.RENAME_ONLY:
        return {}, []
    if mode in {ApplyMode.SAMSUNG_CLEANUP, ApplyMode.GPS_CLEANUP}:
        delete_tags = samsung_cleanup_tags_for_result(result)
        if mode == ApplyMode.GPS_CLEANUP:
            delete_tags.extend(build_gps_datetime_cleanup_tags())
        return {}, delete_tags
    resolved = result.resolved
    if resolved.local_dt is None or resolved.local_date_only:
        return {}, cleanup_tags_for_result(result) if settings.cleanup_enabled else []
    local_str = format_exif_datetime(resolved.local_dt)
    set_tags: dict[str, str] = {}
    if settings.set_filesystem_dates:
        set_tags.update(
            {
                "File:FileCreateDate": local_str,
                "File:FileModifyDate": local_str,
            }
        )
    if settings.write_comment and not has_existing_normalizer_comment(result.metadata.tags, result.item.kind):
        if result.item.kind == MediaKind.IMAGE:
            set_tags["EXIF:UserComment"] = build_normalizer_note(resolved)
        else:
            set_tags["QuickTime:Comment"] = build_normalizer_note(resolved)
    if result.item.kind == MediaKind.IMAGE:
        set_tags.update(
            {
                "EXIF:DateTimeOriginal": local_str,
                "EXIF:CreateDate": local_str,
                "IFD0:ModifyDate": local_str,
            }
        )
        if resolved.offset is not None:
            offset_str = format_offset(resolved.offset)
            set_tags["EXIF:OffsetTimeOriginal"] = offset_str
            set_tags["EXIF:OffsetTime"] = offset_str
            set_tags["EXIF:OffsetTimeDigitized"] = offset_str
    else:
        if resolved.utc_dt is not None:
            utc_str = format_exif_datetime(resolved.utc_dt)
            set_tags.update(
                {
                    "QuickTime:CreateDate": utc_str,
                    "QuickTime:ModifyDate": utc_str,
                    "QuickTime:TrackCreateDate": utc_str,
                    "QuickTime:TrackModifyDate": utc_str,
                    "QuickTime:MediaCreateDate": utc_str,
                    "QuickTime:MediaModifyDate": utc_str,
                    "Keys:CreationTime": format_iso_z(resolved.utc_dt),
                }
            )
        if resolved.offset is not None:
            offset_key = guess_offset_key(result.metadata.tags)
            if offset_key == "Keys:CreationDate":
                set_tags["Keys:CreationDate"] = format_iso_with_offset(resolved.local_dt, resolved.offset)
            elif offset_key == "Keys:SamsungAndroidUtcOffset":
                set_tags["Keys:SamsungAndroidUtcOffset"] = format_offset_compact(resolved.offset)
    delete_tags = cleanup_tags_for_result(result) if settings.cleanup_enabled else []
    return set_tags, delete_tags


