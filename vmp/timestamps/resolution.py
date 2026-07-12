"""Timestamp resolution heuristics and sanity checks.

Builds on :mod:`vmp.timestamps.candidates`, whose public
names are re-exported here for convenient single-module imports.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from ..core.i18n import tr
from ..core.models import (
    Confidence,
    MediaItem,
    MediaKind,
    PlanStatus,
    RawMetadata,
    ResolvedTimestamp,
)
from .candidates import (  # noqa: F401  (re-exported)
    DATETIME_KEY_HINT_RE,
    LOCAL_EXIF_KEYS,
    OFFSET_KEY_HINT_RE,
    OFFSET_KEYS,
    UTC_KEYS,
    ResolutionEvidence,
    TimeCandidate,
    TimeRole,
    build_time_candidates,
    choose_best_local,
    choose_best_offset,
    choose_best_utc,
    choose_date_only,
    collect_datetime_value_pool,
    is_core_metadata_datetime_key,
    is_datetime_like_key,
    is_ignored_datetime_source,
    is_offset_like_key,
    is_utc_biased_key,
    merge_duplicate_candidates,
)
from .parsing import (
    _nearest_quarter_hour_offset,
    get_first_str,
    parse_duration_seconds,
    parse_exif_datetime,
    parse_filename_datetime,
    parse_gps_datetime,
    parse_iso_datetime,
)



def seconds_between(left: datetime, right: datetime) -> int:
    """Return absolute difference in seconds."""
    return int(abs((left - right).total_seconds()))


def max_spread_seconds(values: Sequence[datetime]) -> int:
    """Return max spread in seconds for datetime values."""
    if len(values) < 2:
        return 0
    sorted_values = sorted(values)
    return seconds_between(sorted_values[0], sorted_values[-1])


def is_plausible_timezone_offset(offset: timedelta) -> bool:
    """Return True if an offset looks like a timezone offset."""
    total_minutes = int(offset.total_seconds() // 60)
    abs_minutes = abs(total_minutes)
    if abs_minutes == 0:
        return False
    if abs_minutes > 14 * 60:
        return False
    return abs_minutes % 15 == 0


def infer_from_all_datetime_values(
    metadata: RawMetadata,
    tolerance_seconds: int,
) -> tuple[datetime, datetime, timedelta, tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None:
    """Infer canonical local/UTC/offset from all parsed datetime values."""
    value_counts, value_sources, pair_sources = collect_datetime_value_pool(metadata.tags)
    if len(value_counts) < 2:
        return None
    filename_dt = parse_filename_datetime(Path(metadata.source_file).name)
    best_pair: tuple[datetime, datetime, int] | None = None
    best_score = -1
    for pair_key, sources in pair_sources.items():
        local_dt, utc_dt, offset_seconds = pair_key
        offset_td = timedelta(seconds=offset_seconds)
        if not is_plausible_timezone_offset(offset_td):
            continue
        score = len(sources) * 10 + value_counts.get(local_dt, 0) + value_counts.get(utc_dt, 0)
        if filename_dt is not None and seconds_between(local_dt, filename_dt) <= tolerance_seconds:
            score += 8
        if score > best_score:
            best_score = score
            best_pair = pair_key
    if best_pair is not None:
        local_dt, utc_dt, offset_seconds = best_pair
        return (
            local_dt,
            utc_dt,
            timedelta(seconds=offset_seconds),
            tuple(sorted(value_sources.get(local_dt, set()))),
            tuple(sorted(value_sources.get(utc_dt, set()))),
            tuple(sorted(pair_sources.get(best_pair, set()))),
        )
    unique_values = sorted(value_counts.keys())
    best_triplet: tuple[datetime, datetime, timedelta] | None = None
    best_triplet_score = -1
    for left_index in range(len(unique_values)):
        for right_index in range(left_index + 1, len(unique_values)):
            a = unique_values[left_index]
            b = unique_values[right_index]
            diff = b - a
            if not is_plausible_timezone_offset(diff):
                continue
            score_a = value_counts.get(a, 0) + value_counts.get(b, 0)
            if filename_dt is not None and seconds_between(b, filename_dt) <= tolerance_seconds:
                score_a += 8
            score_b = value_counts.get(a, 0) + value_counts.get(b, 0)
            if filename_dt is not None and seconds_between(a, filename_dt) <= tolerance_seconds:
                score_b += 8
            if score_a >= score_b and score_a > best_triplet_score:
                best_triplet_score = score_a
                best_triplet = (b, a, diff)
            elif score_b > score_a and score_b > best_triplet_score:
                best_triplet_score = score_b
                best_triplet = (a, b, -diff)
    if best_triplet is None:
        return None
    local_dt, utc_dt, offset_td = best_triplet
    local_sources = tuple(sorted(value_sources.get(local_dt, set())))
    utc_sources = tuple(sorted(value_sources.get(utc_dt, set())))
    return local_dt, utc_dt, offset_td, local_sources, utc_sources, tuple(sorted(set((*local_sources, *utc_sources))))


def maybe_derive_offset(local_dt: datetime, utc_dt: datetime, tolerance_seconds: int) -> timedelta | None:
    """Derive a plausible offset from local and UTC datetimes."""
    raw_offset = local_dt - utc_dt
    rounded_minutes = int(round(raw_offset.total_seconds() / 60.0))
    rounded_offset = timedelta(minutes=rounded_minutes)
    drift_seconds = abs(int((raw_offset - rounded_offset).total_seconds()))
    if drift_seconds > tolerance_seconds:
        return None
    total_minutes = abs(rounded_minutes)
    if total_minutes == 0:
        return timedelta(0)
    # Real timezone offsets never exceed ±14:00. A larger "offset" means the
    # local/UTC pair is mismatched (e.g. a container UTC time paired with a
    # filename local time across a date boundary), not a timezone — reject it
    # instead of writing a bogus OffsetTime.
    if total_minutes > 14 * 60:
        return None
    if total_minutes % 15 == 0:
        return rounded_offset
    return None


def infer_local_utc_offset_from_spread(
    candidates: Sequence[TimeCandidate],
    tolerance_seconds: int,
) -> tuple[datetime, datetime, timedelta, tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None:
    """Infer local/UTC/offset from min/max datetime spread and UTC evidence."""
    dt_candidates = [c for c in candidates if c.dt is not None and c.role in {TimeRole.LOCAL, TimeRole.UTC}]
    if len(dt_candidates) < 2:
        return None
    dt_to_sources: dict[datetime, set[str]] = {}
    for candidate in dt_candidates:
        assert candidate.dt is not None
        dt_to_sources.setdefault(candidate.dt, set()).update(candidate.source_tags)
    unique_dts = sorted(dt_to_sources.keys())
    if len(unique_dts) < 2:
        return None
    oldest = unique_dts[0]
    newest = unique_dts[-1]
    spread = newest - oldest
    if not is_plausible_timezone_offset(spread):
        return None
    utc_candidates = [c for c in candidates if c.role == TimeRole.UTC and c.dt is not None]
    if not utc_candidates:
        return None
    best_utc = max(utc_candidates, key=lambda item: item.score)
    assert best_utc.dt is not None
    if seconds_between(best_utc.dt, oldest) <= tolerance_seconds:
        return newest, oldest, spread, tuple(sorted(dt_to_sources[newest])), tuple(sorted(dt_to_sources[oldest])), best_utc.source_tags
    if seconds_between(best_utc.dt, newest) <= tolerance_seconds:
        return oldest, newest, -spread, tuple(sorted(dt_to_sources[oldest])), tuple(sorted(dt_to_sources[newest])), best_utc.source_tags
    return None


_UNSET: Any = object()


def resolve_timestamp(
    item: MediaItem,
    metadata: RawMetadata,
    tolerance_seconds: int,
    candidates: list[TimeCandidate] | None = None,
    inferred_all: tuple | None = _UNSET,
) -> ResolvedTimestamp:
    """Resolve canonical local time, offset, UTC time, confidence, and evidence.

    ``candidates`` and ``inferred_all`` may be precomputed (see
    :func:`vmp.metadata.analyze_item`) so resolve and
    sanity evaluation share one pass over the tags instead of re-parsing every
    datetime value per file.
    """
    if candidates is None:
        candidates = build_time_candidates(metadata, item.kind)
    evidence = ResolutionEvidence()
    best_local = choose_best_local(candidates, item.kind)
    best_offset = choose_best_offset(candidates)
    best_utc = choose_best_utc(candidates)
    best_date_only = choose_date_only(candidates)
    local_dt: datetime | None = None
    utc_dt: datetime | None = None
    offset: timedelta | None = None
    local_date_only = False
    local_sources: tuple[str, ...] = ()
    offset_sources: tuple[str, ...] = ()
    utc_sources: tuple[str, ...] = ()
    if inferred_all is _UNSET:
        inferred_all = infer_from_all_datetime_values(metadata, tolerance_seconds)
    # A real capture-time tag (DateTimeOriginal, CreateDate, Keys:CreationDate, ...)
    # must win over the cross-value LOCAL/UTC/offset heuristic. The heuristic pairs
    # any two datetime values whose difference happens to look like a timezone
    # offset, so a photo edited an hour after capture (DateTimeOriginal 12:00 +
    # ModifyDate 13:00) would otherwise be assigned the edit time as LOCAL. When a
    # non-filename local capture tag disagrees with the inferred local time, the
    # inference is untrustworthy: drop it and fall back to the real tag below.
    if (
        inferred_all is not None
        and best_local is not None
        and best_local.dt is not None
        and best_local.label != "filename_datetime"
        and seconds_between(inferred_all[0], best_local.dt) > tolerance_seconds
    ):
        evidence.notes.append(
            "Ignored cross-value LOCAL/UTC inference because it contradicts a real capture-time tag."
        )
        inferred_all = None
    if inferred_all is not None:
        local_dt, utc_dt, offset, local_sources, utc_sources, offset_sources = inferred_all
        evidence.matched_tags.extend((*local_sources, *utc_sources, *offset_sources))
        evidence.notes.append("Derived canonical LOCAL/UTC/OFFSET from all parsed datetime values.")
    if best_local is not None:
        if local_dt is None:
            local_dt = best_local.dt
            local_sources = best_local.source_tags
            evidence.matched_tags.extend(best_local.source_tags)
        if best_local.label == "filename_datetime":
            evidence.notes.append("Using filename datetime as local time source.")
        if offset is None and best_local.offset is not None:
            offset = best_local.offset
            offset_sources = best_local.source_tags
            evidence.notes.append(f"Offset embedded in {best_local.label}.")
    if offset is None and best_offset is not None:
        offset = best_offset.offset
        offset_sources = best_offset.source_tags
        evidence.matched_tags.extend(best_offset.source_tags)
    if best_utc is not None:
        utc_dt = best_utc.dt
        utc_sources = best_utc.source_tags
        evidence.matched_tags.extend(best_utc.source_tags)
    if local_dt is None and item.kind == MediaKind.VIDEO and utc_dt is not None and offset is not None:
        local_dt = utc_dt + offset
        local_sources = local_sources or utc_sources or offset_sources
        evidence.notes.append("Derived local time from UTC plus offset.")
    if local_dt is not None and utc_dt is None and offset is not None:
        utc_dt = local_dt - offset
        utc_sources = utc_sources or local_sources or offset_sources
        evidence.notes.append("Derived UTC from local time plus offset.")
    if local_dt is not None and utc_dt is not None:
        derived_offset = maybe_derive_offset(local_dt, utc_dt, tolerance_seconds)
        if derived_offset is not None:
            should_replace = offset is None
            if offset is not None:
                delta = abs(int((offset - derived_offset).total_seconds()))
                should_replace = delta > tolerance_seconds or (offset == timedelta(0) and derived_offset != timedelta(0))
            if should_replace:
                offset = derived_offset
                offset_sources = offset_sources or local_sources or utc_sources
                evidence.notes.append("Derived offset from local time minus UTC.")
    if local_dt is None:
        unique_naive_values: set[datetime] = set()
        unique_sources: set[str] = set()
        has_real_offset = False
        for candidate in candidates:
            if candidate.dt is not None and candidate.role in {TimeRole.LOCAL, TimeRole.UTC}:
                unique_naive_values.add(candidate.dt)
                unique_sources.update(candidate.source_tags)
            if candidate.offset is not None and candidate.offset != timedelta(0):
                has_real_offset = True
        if len(unique_naive_values) == 1 and not has_real_offset:
            local_dt = next(iter(unique_naive_values))
            utc_dt = None
            offset = None
            local_sources = tuple(sorted(unique_sources))
            offset_sources = ()
            utc_sources = ()
            evidence.notes.append("Single datetime without offset treated as local time.")
    spread_inference = infer_local_utc_offset_from_spread(candidates, tolerance_seconds)
    if spread_inference is not None:
        inferred_local, inferred_utc, inferred_offset, inferred_local_sources, inferred_utc_sources, inferred_offset_sources = spread_inference
        if local_dt is None or utc_dt is None or offset is None or not is_plausible_timezone_offset(offset):
            local_dt = inferred_local
            utc_dt = inferred_utc
            offset = inferred_offset
            local_sources = inferred_local_sources or local_sources
            utc_sources = inferred_utc_sources or utc_sources
            offset_sources = inferred_offset_sources or offset_sources
            evidence.notes.append("Inferred local/UTC/offset from datetime spread and UTC-tag alignment.")
    if local_dt is None and best_date_only is not None:
        local_dt = best_date_only.dt
        local_date_only = True
        local_sources = best_date_only.source_tags
        evidence.matched_tags.extend(best_date_only.source_tags)
        evidence.notes.append("Only a date could be extracted from the filename.")
    # Samsung (and similar) write the container CreateDate at the moment the
    # recording is finalized, i.e. the END of the clip in UTC, while the filename
    # holds the START in local time. Detect this via the duration: if shifting the
    # container time back by the duration yields a clean timezone offset against
    # the local time (but the unshifted container does not), the container is the
    # end — correct UTC to the true capture start.
    duration_corroborated = False
    if item.kind == MediaKind.VIDEO and local_dt is not None and utc_dt is not None:
        duration_seconds = parse_duration_seconds(metadata.tags)
        if duration_seconds is not None and duration_seconds >= 3:
            # Offset implied if the container holds the START vs the END of the clip.
            raw_start = (local_dt - utc_dt).total_seconds()
            raw_end = raw_start + duration_seconds
            offset_if_end, drift_end = _nearest_quarter_hour_offset(raw_end)
            _, drift_start = _nearest_quarter_hour_offset(raw_start)
            # The container is the end-of-recording when shifting it back by the
            # duration lands almost exactly on a real timezone offset and explains
            # at least ~5s that the unshifted container could not.
            if (
                drift_end <= 5.0
                and drift_start - drift_end >= 5.0
                and is_plausible_timezone_offset(offset_if_end)
            ):
                utc_dt = local_dt - offset_if_end
                offset = offset_if_end
                offset_sources = offset_sources or local_sources or utc_sources
                duration_corroborated = True
                evidence.notes.append(
                    "Container timestamp is end-of-recording; corrected UTC to capture start using video duration."
                )
    if local_dt is None:
        confidence = Confidence.ZERO
        evidence.warnings.append("No usable local capture datetime could be resolved.")
    elif offset is not None:
        confidence = Confidence.HIGH if utc_dt is not None else Confidence.MEDIUM
    else:
        confidence = Confidence.MEDIUM if not local_date_only else Confidence.LOW
    # A filename start time counts as corroborated when a real (non-derived)
    # container timestamp agrees with it — either directly (container = start) or
    # after shifting the container back by the duration (container = end).
    container_utc_value = best_utc.dt if best_utc is not None else None
    direct_corroborated = (
        not duration_corroborated
        and container_utc_value is not None
        and local_dt is not None
        and offset is not None
        and seconds_between(container_utc_value, local_dt - offset) <= tolerance_seconds
    )
    if item.kind == MediaKind.VIDEO and "System:FileName" in local_sources:
        if duration_corroborated or direct_corroborated:
            confidence = Confidence.HIGH
            if duration_corroborated:
                evidence.notes.append("Filename start time corroborated by container end minus duration.")
            else:
                evidence.notes.append("Filename start time corroborated by container timestamp.")
        else:
            confidence = Confidence.HIGH if confidence == Confidence.MEDIUM else Confidence.MEDIUM
            evidence.notes.append("Android filename pattern treated as authoritative local time.")
    source = ", ".join(dict.fromkeys(local_sources)) or "unresolved"
    if item.kind == MediaKind.VIDEO and "System:FileName" in local_sources:
        if duration_corroborated:
            source += tr(" (per Videolänge bestätigt)")
        elif direct_corroborated:
            source += tr(" (Container bestätigt)")
    return ResolvedTimestamp(
        local_dt=local_dt,
        utc_dt=utc_dt,
        offset=offset,
        confidence=confidence,
        source=source,
        warnings=evidence.warnings,
        notes=evidence.notes,
        local_date_only=local_date_only,
        local_sources=local_sources,
        offset_sources=offset_sources,
        utc_sources=utc_sources,
    )


def evaluate_sanity(
    resolved: ResolvedTimestamp,
    metadata: RawMetadata,
    kind: MediaKind,
    tolerance_seconds: int,
    candidates: list[TimeCandidate] | None = None,
    inferred_all: tuple | None = _UNSET,
) -> tuple[PlanStatus, list[str]]:
    """Evaluate resolved times for conflicts and warnings."""
    tags = metadata.tags
    warnings: list[str] = []
    if resolved.local_dt is None:
        return PlanStatus.SKIP, ["No usable local capture time could be resolved."]
    if resolved.local_date_only:
        warnings.append("Only a date was found; no real capture time is available.")
    all_candidates = candidates if candidates is not None else build_time_candidates(metadata, kind)
    if inferred_all is _UNSET:
        inferred_all = infer_from_all_datetime_values(metadata, tolerance_seconds)
    strong_local_values = sorted(
        {
            c.dt
            for c in all_candidates
            if c.role == TimeRole.LOCAL and c.dt is not None and c.score >= 85
        }
    )
    strong_utc_values = sorted(
        {
            c.dt
            for c in all_candidates
            if c.role == TimeRole.UTC and c.dt is not None and c.score >= 80
        }
    )
    if len(strong_local_values) >= 2:
        # Conflicting real local capture tags (e.g. DateTimeOriginal vs a later
        # ModifyDate) matter even when an offset could be inferred, because the
        # resolver now trusts the real capture tag over the cross-value guess.
        local_spread = max_spread_seconds(strong_local_values)
        if local_spread > tolerance_seconds:
            warnings.append(f"Conflicting local metadata values (spread {local_spread} seconds).")
    if inferred_all is None and strong_utc_values:
        utc_spread = max_spread_seconds(strong_utc_values)
        if utc_spread > tolerance_seconds:
            warnings.append(f"Conflicting UTC metadata values (spread {utc_spread} seconds).")
    primary_local = get_first_str(tags, *LOCAL_EXIF_KEYS)
    if primary_local:
        parsed_iso = parse_iso_datetime(primary_local)
        parsed_exif = parse_exif_datetime(primary_local)
        local_check = parsed_iso[0] if parsed_iso is not None else parsed_exif
        if local_check is not None:
            delta = seconds_between(local_check, resolved.local_dt)
            if delta > tolerance_seconds:
                warnings.append(f"Primary local timestamp differs by {delta} seconds.")
    filename_dt = parse_filename_datetime(Path(metadata.source_file).name)
    if filename_dt is not None:
        delta = seconds_between(filename_dt, resolved.local_dt)
        if delta > tolerance_seconds:
            warnings.append(f"Filename timestamp differs by {delta} seconds.")
    gps_date = get_first_str(tags, "GPS:GPSDateStamp", "GPS:GPSDate")
    gps_time = get_first_str(tags, "GPS:GPSTimeStamp")
    if gps_date and gps_time and resolved.utc_dt is not None:
        gps_dt = parse_gps_datetime(gps_date, gps_time)
        if gps_dt is not None:
            delta = seconds_between(gps_dt, resolved.utc_dt)
            if delta > tolerance_seconds:
                warnings.append(f"GPS UTC differs by {delta} seconds.")
    if kind == MediaKind.VIDEO and resolved.utc_dt is None:
        warnings.append("Video resolved without a usable UTC timestamp.")
    return (PlanStatus.WARN, warnings) if warnings else (PlanStatus.OK, warnings)
