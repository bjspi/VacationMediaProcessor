"""Pure suggestion and data models for repairing missing media GPS positions."""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from .core.i18n import tr
from .core.models import AnalysisResult, Confidence, GpsRepairSettings, MediaKind, RawMetadata
from .metadata import gps_coordinates
from .timestamps.parsing import get_first_str

EARTH_RADIUS_KM = 6371.0088
_TIME_CONFIDENCE = {Confidence.ZERO: 0, Confidence.LOW: 1, Confidence.MEDIUM: 2, Confidence.HIGH: 3}


class GpsSuggestionStatus(str, Enum):
    """User-facing confidence bucket for one missing-GPS record."""

    SAFE = "safe"
    REVIEW = "review"
    MANUAL = "manual"


class GpsSuggestionMethod(str, Enum):
    """How a proposed or assigned position was produced."""

    NONE = "none"
    SINGLE_ANCHOR = "single_anchor"
    INTERPOLATED = "interpolated"
    CLUSTER_SUPPORTED = "cluster_supported"
    MANUAL = "manual"


@dataclass(slots=True)
class GpsRepairRecord:
    """Small immutable-like view of scan metadata used by the repair feature."""

    path: Path
    root: Path
    kind: MediaKind
    local_dt: datetime | None
    confidence: Confidence
    date_only: bool
    lat: float | None = None
    lon: float | None = None
    make: str = ""
    model: str = ""
    metadata: RawMetadata | None = None

    @property
    def has_gps(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def device_key(self) -> str:
        make = " ".join(self.make.casefold().split())
        model = " ".join(self.model.casefold().split())
        return f"{make}|{model}" if make and model else ""

    @property
    def has_reliable_time(self) -> bool:
        return (
            self.local_dt is not None
            and not self.date_only
            and _TIME_CONFIDENCE.get(self.confidence, 0) >= _TIME_CONFIDENCE[Confidence.MEDIUM]
        )


@dataclass(slots=True)
class GpsSuggestion:
    """Computed proposal for one GPS-less media record."""

    target: GpsRepairRecord
    status: GpsSuggestionStatus
    method: GpsSuggestionMethod = GpsSuggestionMethod.NONE
    lat: float | None = None
    lon: float | None = None
    anchor_paths: tuple[Path, ...] = ()
    summary: str = ""
    details: tuple[str, ...] = ()

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass(slots=True)
class GpsAssignment:
    """One confirmed coordinate ready for the standalone writer."""

    target: GpsRepairRecord
    lat: float
    lon: float
    method: GpsSuggestionMethod
    anchor_paths: tuple[Path, ...] = ()


@dataclass(slots=True)
class GpsRepairEntryResult:
    """Outcome for one attempted GPS metadata write."""

    assignment: GpsAssignment
    success: bool = False
    skipped: bool = False
    backup_path: Path | None = None
    error: str = ""
    readback: RawMetadata | None = None


@dataclass(slots=True)
class GpsRepairReport:
    """Result of one standalone GPS repair batch."""

    run_id: str
    entries: list[GpsRepairEntryResult] = field(default_factory=list)
    manifest_paths: list[Path] = field(default_factory=list)
    cancelled: bool = False

    @property
    def changed(self) -> int:
        return sum(entry.success for entry in self.entries)

    @property
    def failed(self) -> int:
        return sum(bool(entry.error) for entry in self.entries)


@dataclass(slots=True)
class _AnchorCluster:
    anchors: tuple[GpsRepairRecord, ...]

    @property
    def start(self) -> datetime:
        assert self.anchors[0].local_dt is not None
        return self.anchors[0].local_dt

    @property
    def end(self) -> datetime:
        assert self.anchors[-1].local_dt is not None
        return self.anchors[-1].local_dt


def record_from_result(result: AnalysisResult) -> GpsRepairRecord:
    """Build a repair record from an existing scan result."""
    tags = result.metadata.tags
    coords = gps_coordinates(tags)
    lat, lon = coords if coords is not None else (None, None)
    make = get_first_str(
        tags,
        "EXIF:Make",
        "IFD0:Make",
        "QuickTime:Make",
        "Keys:Make",
        "MakerNotes:Make",
    ) or ""
    model = get_first_str(
        tags,
        "EXIF:Model",
        "IFD0:Model",
        "QuickTime:Model",
        "Keys:Model",
        "MakerNotes:Model",
        "QuickTime:DeviceModelName",
    ) or ""
    return GpsRepairRecord(
        path=result.item.path,
        root=result.item.root,
        kind=result.item.kind,
        local_dt=result.resolved.local_dt,
        confidence=result.resolved.confidence,
        date_only=result.resolved.local_date_only,
        lat=lat,
        lon=lon,
        make=make,
        model=model,
        metadata=result.metadata,
    )


def haversine_km(left: tuple[float, float], right: tuple[float, float]) -> float:
    """Return great-circle distance between decimal latitude/longitude pairs."""
    lat1, lon1 = map(math.radians, left)
    lat2, lon2 = map(math.radians, right)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1.0 - value)))


def interpolate_position(
    left: tuple[float, float], right: tuple[float, float], fraction: float
) -> tuple[float, float]:
    """Spherically interpolate a position, handling short and dateline-crossing paths."""
    fraction = max(0.0, min(1.0, fraction))
    if fraction <= 0:
        return left
    if fraction >= 1:
        return right

    def _vector(point: tuple[float, float]) -> tuple[float, float, float]:
        lat, lon = map(math.radians, point)
        return (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))

    one = _vector(left)
    two = _vector(right)
    dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(one, two, strict=True))))
    omega = math.acos(dot)
    if abs(omega) < 1e-9:
        return left
    sin_omega = math.sin(omega)
    if abs(sin_omega) < 1e-9:
        lat = left[0] + (right[0] - left[0]) * fraction
        lon = left[1] + (right[1] - left[1]) * fraction
        return (lat, ((lon + 180.0) % 360.0) - 180.0)
    a = math.sin((1.0 - fraction) * omega) / sin_omega
    b = math.sin(fraction * omega) / sin_omega
    x = a * one[0] + b * two[0]
    y = a * one[1] + b * two[1]
    z = a * one[2] + b * two[2]
    return (math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x)))


def _median_center(records: list[GpsRepairRecord]) -> tuple[float, float]:
    latitudes = sorted(record.lat for record in records if record.lat is not None)
    longitudes = sorted(record.lon for record in records if record.lon is not None)
    middle = len(latitudes) // 2
    if len(latitudes) % 2:
        return (latitudes[middle], longitudes[middle])
    return (
        (latitudes[middle - 1] + latitudes[middle]) / 2,
        (longitudes[middle - 1] + longitudes[middle]) / 2,
    )


def _within_radius(records: list[GpsRepairRecord], radius_m: int) -> bool:
    center = _median_center(records)
    return all(
        haversine_km(center, (record.lat, record.lon)) * 1000 <= radius_m  # type: ignore[arg-type]
        for record in records
    )


def _find_clusters(anchors: list[GpsRepairRecord], settings: GpsRepairSettings) -> list[_AnchorCluster]:
    """Find contiguous stationary anchor runs without allowing radius chaining."""
    clusters: list[_AnchorCluster] = []
    current: list[GpsRepairRecord] = []

    def _finish(records: list[GpsRepairRecord]) -> None:
        if len(records) < settings.cluster_min_anchors:
            return
        assert records[0].local_dt is not None and records[-1].local_dt is not None
        span_minutes = (records[-1].local_dt - records[0].local_dt).total_seconds() / 60
        if span_minutes >= settings.cluster_min_span_minutes:
            candidate = _AnchorCluster(tuple(records))
            if not clusters or candidate.anchors != clusters[-1].anchors:
                clusters.append(candidate)

    for anchor in anchors:
        if not current:
            current = [anchor]
            continue
        assert anchor.local_dt is not None and current[-1].local_dt is not None
        gap_minutes = (anchor.local_dt - current[-1].local_dt).total_seconds() / 60
        trial = [*current, anchor]
        if gap_minutes <= settings.cluster_max_gap_minutes and _within_radius(trial, settings.cluster_radius_m):
            current = trial
            continue
        _finish(current)
        overlap = [current[-1], anchor]
        current = overlap if gap_minutes <= settings.cluster_max_gap_minutes and _within_radius(overlap, settings.cluster_radius_m) else [anchor]
    _finish(current)
    return clusters


def _bracket(
    anchors: list[GpsRepairRecord], moment: datetime
) -> tuple[GpsRepairRecord | None, GpsRepairRecord | None]:
    moments = [anchor.local_dt for anchor in anchors]
    index = bisect_left(moments, moment)  # type: ignore[arg-type]
    before = anchors[index - 1] if index > 0 else None
    after = anchors[index] if index < len(anchors) else None
    return before, after


def surrounding_anchors(
    records: list[GpsRepairRecord], target: GpsRepairRecord, count_each_side: int = 2
) -> list[GpsRepairRecord]:
    """Return map context anchors around a target, with date-only fallbacks."""
    anchors = sorted(
        (record for record in records if record.has_gps and record.local_dt is not None),
        key=lambda record: record.local_dt,  # type: ignore[arg-type,return-value]
    )
    if target.local_dt is None:
        return anchors
    if target.date_only:
        same_day = [anchor for anchor in anchors if anchor.local_dt and anchor.local_dt.date() == target.local_dt.date()]
        return same_day or anchors
    moments = [anchor.local_dt for anchor in anchors]
    index = bisect_left(moments, target.local_dt)  # type: ignore[arg-type]
    return anchors[max(0, index - count_each_side) : index + count_each_side]


def _same_device(target: GpsRepairRecord, anchors: tuple[GpsRepairRecord, ...]) -> bool:
    return bool(target.device_key) and all(anchor.device_key == target.device_key for anchor in anchors)


def _interpolated(
    target: GpsRepairRecord,
    before: GpsRepairRecord,
    after: GpsRepairRecord,
    settings: GpsRepairSettings,
    *,
    cluster_supported: bool = False,
) -> GpsSuggestion:
    assert target.local_dt is not None and before.local_dt is not None and after.local_dt is not None
    assert before.lat is not None and before.lon is not None and after.lat is not None and after.lon is not None
    gap_seconds = (after.local_dt - before.local_dt).total_seconds()
    if gap_seconds <= 0:
        return _manual(target, tr("Manuell · widersprüchliche Ankerzeiten"))
    fraction = (target.local_dt - before.local_dt).total_seconds() / gap_seconds
    if not 0 <= fraction <= 1:
        return _manual(target, tr("Manuell · keine umschließenden Anker"))
    gap_minutes = gap_seconds / 60
    distance_km = haversine_km((before.lat, before.lon), (after.lat, after.lon))
    speed_kmh = distance_km / (gap_seconds / 3600)
    within_review = (
        gap_minutes <= max(settings.review_pair_max_hours * 60, settings.pair_safe_minutes)
        and distance_km <= max(settings.review_pair_max_distance_km, settings.pair_safe_distance_km)
    )
    if not within_review:
        return _manual(target, tr("Manuell · Anker zu weit auseinander"))
    lat, lon = interpolate_position((before.lat, before.lon), (after.lat, after.lon), fraction)
    same_device = _same_device(target, (before, after))
    safe = cluster_supported or (
        same_device
        and gap_minutes <= settings.pair_safe_minutes
        and distance_km <= settings.pair_safe_distance_km
        and speed_kmh <= settings.pair_safe_speed_kmh
    )
    status = GpsSuggestionStatus.SAFE if safe else GpsSuggestionStatus.REVIEW
    method = GpsSuggestionMethod.CLUSTER_SUPPORTED if cluster_supported else GpsSuggestionMethod.INTERPOLATED
    label = tr("Sicher") if safe else tr("Prüfen")
    cluster_text = tr(" · Standortcluster") if cluster_supported else ""
    summary = tr("{label} · {minutes:.0f} min · {distance:.1f} km{cluster}").format(
        label=label, minutes=gap_minutes, distance=distance_km, cluster=cluster_text
    )
    details = (
        tr("Vorher: {name} ({time})").format(name=before.path.name, time=before.local_dt.strftime("%H:%M:%S")),
        tr("Nachher: {name} ({time})").format(name=after.path.name, time=after.local_dt.strftime("%H:%M:%S")),
        tr("Mittlere Bewegung: {speed:.1f} km/h").format(speed=speed_kmh),
        tr("Gerät: {state}").format(state=tr("gleich") if same_device else tr("übergreifend")),
    )
    return GpsSuggestion(target, status, method, lat, lon, (before.path, after.path), summary, details)


def _single(
    target: GpsRepairRecord,
    anchor: GpsRepairRecord,
    settings: GpsRepairSettings,
) -> GpsSuggestion:
    assert target.local_dt is not None and anchor.local_dt is not None and anchor.lat is not None and anchor.lon is not None
    delta_minutes = abs((target.local_dt - anchor.local_dt).total_seconds()) / 60
    if delta_minutes > max(settings.review_single_max_minutes, settings.single_safe_minutes):
        return _manual(target, tr("Manuell · einzelner Anker zu weit entfernt"))
    same_device = _same_device(target, (anchor,))
    safe = same_device and delta_minutes <= settings.single_safe_minutes
    status = GpsSuggestionStatus.SAFE if safe else GpsSuggestionStatus.REVIEW
    label = tr("Sicher") if safe else tr("Prüfen")
    return GpsSuggestion(
        target,
        status,
        GpsSuggestionMethod.SINGLE_ANCHOR,
        anchor.lat,
        anchor.lon,
        (anchor.path,),
        tr("{label} · ein Anker · {minutes:.1f} min").format(label=label, minutes=delta_minutes),
        (
            tr("Anker: {name} ({time})").format(name=anchor.path.name, time=anchor.local_dt.strftime("%H:%M:%S")),
            tr("Gerät: {state}").format(state=tr("gleich") if same_device else tr("übergreifend")),
        ),
    )


def _manual(target: GpsRepairRecord, summary: str) -> GpsSuggestion:
    return GpsSuggestion(target=target, status=GpsSuggestionStatus.MANUAL, summary=summary)


def build_gps_suggestions(
    records: list[GpsRepairRecord], settings: GpsRepairSettings
) -> list[GpsSuggestion]:
    """Build deterministic suggestions for every record without usable GPS."""
    reliable_anchors = sorted(
        (record for record in records if record.has_gps and record.has_reliable_time),
        key=lambda record: record.local_dt,  # type: ignore[arg-type,return-value]
    )
    clusters = _find_clusters(reliable_anchors, settings)
    targets = [record for record in records if not record.has_gps]
    suggestions: list[GpsSuggestion] = []
    for target in targets:
        if not target.has_reliable_time:
            summary = (
                tr("Manuell · nur Datumsangabe")
                if target.date_only
                else tr("Manuell · keine verlässliche Uhrzeit")
            )
            suggestions.append(_manual(target, summary))
            continue
        assert target.local_dt is not None

        cluster_match: tuple[_AnchorCluster, GpsRepairRecord, GpsRepairRecord] | None = None
        for cluster in clusters:
            if not cluster.start <= target.local_dt <= cluster.end:
                continue
            before, after = _bracket(list(cluster.anchors), target.local_dt)
            if before is not None and after is not None:
                cluster_match = (cluster, before, after)
                break
        if cluster_match is not None:
            _, before, after = cluster_match
            suggestions.append(_interpolated(target, before, after, settings, cluster_supported=True))
            continue

        global_before, global_after = _bracket(reliable_anchors, target.local_dt)
        chosen_before, chosen_after = global_before, global_after
        if target.device_key:
            device_anchors = [anchor for anchor in reliable_anchors if anchor.device_key == target.device_key]
            device_before, device_after = _bracket(device_anchors, target.local_dt)
            if device_before is not None and device_after is not None:
                device_candidate = _interpolated(target, device_before, device_after, settings)
                if device_candidate.status != GpsSuggestionStatus.MANUAL:
                    suggestions.append(device_candidate)
                    continue
            if not (global_before is not None and global_after is not None):
                chosen_before = device_before or global_before
                chosen_after = device_after or global_after

        if chosen_before is not None and chosen_after is not None:
            suggestions.append(_interpolated(target, chosen_before, chosen_after, settings))
        elif chosen_before is not None or chosen_after is not None:
            suggestions.append(_single(target, chosen_before or chosen_after, settings))  # type: ignore[arg-type]
        else:
            suggestions.append(_manual(target, tr("Manuell · keine GPS-Anker")))

    return sorted(
        suggestions,
        key=lambda suggestion: (
            suggestion.target.local_dt is None,
            suggestion.target.local_dt or datetime.max,
            str(suggestion.target.path).casefold(),
        ),
    )


__all__ = [
    "GpsAssignment",
    "GpsRepairEntryResult",
    "GpsRepairRecord",
    "GpsRepairReport",
    "GpsSuggestion",
    "GpsSuggestionMethod",
    "GpsSuggestionStatus",
    "build_gps_suggestions",
    "haversine_km",
    "interpolate_position",
    "record_from_result",
    "surrounding_anchors",
]
