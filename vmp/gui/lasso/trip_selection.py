"""Pure spatial + temporal selection logic for the Trip Lasso feature.

No Qt imports — fully unit-testable. The GUI builds lightweight :class:`TripRecord`
values from :class:`~vmp.core.models.AnalysisResult` objects and
feeds them here. Two entry points produce a :class:`TripSelection`:

* :func:`select_by_polygon` — map mode: GPS points inside the drawn polygon form
  the *anchors*; their capture times define a whole-day window; media without GPS
  whose capture time falls inside that window are auto-included (time bridging).
* :func:`select_by_date_range` — date mode: every record whose capture time lies
  in the given window, regardless of GPS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

# A polygon is an ordered list of (latitude, longitude) vertices.
Polygon = list[tuple[float, float]]


@dataclass(slots=True)
class TripRecord:
    """One selectable media item, reduced to what selection needs."""

    path: Path
    lat: float | None
    lon: float | None
    local_dt: datetime | None
    date_only: bool = False
    kind: str = "image"  # "image" | "video"

    @property
    def has_gps(self) -> bool:
        """Return True when usable coordinates are present."""
        return self.lat is not None and self.lon is not None


@dataclass(slots=True)
class TripSelection:
    """Result of a spatial or temporal selection pass."""

    included: list[TripRecord] = field(default_factory=list)
    anchors: list[TripRecord] = field(default_factory=list)
    edge_day: set[Path] = field(default_factory=set)
    window: tuple[datetime, datetime] | None = None
    # Records that could not be placed (no usable capture time, and — in map
    # mode — no GPS either). Surfaced for review rather than silently dropped.
    unplaceable: list[TripRecord] = field(default_factory=list)


@dataclass(slots=True)
class DayBucket:
    """Aggregated media count for one capture day in histogram mode."""

    day: date
    total: int = 0
    images: int = 0
    videos: int = 0
    gps_count: int = 0


def point_in_polygon(lat: float, lon: float, polygon: Polygon) -> bool:
    """Return True if (lat, lon) lies inside ``polygon`` (ray-casting).

    The polygon is a list of (lat, lon) vertices; it is treated as closed. Points
    exactly on an edge are reported as inside on a best-effort basis. Polygons
    crossing the antimeridian or the poles are not supported.
    """
    if len(polygon) < 3:
        return False
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        yi, xi = polygon[i]  # (lat, lon)
        yj, xj = polygon[j]
        # Does the horizontal ray at `lat` cross the edge (i, j)?
        intersects = (yi > lat) != (yj > lat)
        if intersects:
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _day_window(moments: list[datetime]) -> tuple[datetime, datetime]:
    """Return [start-of-first-day, end-of-last-day] covering all ``moments``."""
    start_day = min(moments).date()
    end_day = max(moments).date()
    return (
        datetime.combine(start_day, time.min),
        datetime.combine(end_day, time.max),
    )


def _within(dt: datetime | None, window: tuple[datetime, datetime]) -> bool:
    """Return True if ``dt`` is non-None and falls inside ``window`` (inclusive)."""
    return dt is not None and window[0] <= dt <= window[1]


def _edge_day_paths(records: list[TripRecord], window: tuple[datetime, datetime]) -> set[Path]:
    """Return paths of records captured on the first or last day of ``window``."""
    first_day = window[0].date()
    last_day = window[1].date()
    edge: set[Path] = set()
    for record in records:
        if record.local_dt is None:
            continue
        day = record.local_dt.date()
        if day == first_day or day == last_day:
            edge.add(record.path)
    return edge


def _sort_key(record: TripRecord) -> tuple[int, datetime | str]:
    """Sort placed records chronologically, undated ones last by path."""
    if record.local_dt is None:
        return (1, str(record.path).casefold())
    return (0, record.local_dt)


def select_by_polygon(records: list[TripRecord], polygon: Polygon) -> TripSelection:
    """Select a trip from a map polygon plus automatic time bridging.

    Anchors are GPS-bearing records inside the polygon. Their capture times define
    a whole-day window; GPS-less records whose capture time lies in that window are
    auto-included. GPS-bearing records outside the polygon stay excluded even when
    their time falls in the window (they were somewhere else).
    """
    anchors = [r for r in records if r.has_gps and point_in_polygon(r.lat, r.lon, polygon)]  # type: ignore[arg-type]
    anchor_times = [r.local_dt for r in anchors if r.local_dt is not None]

    if not anchor_times:
        # No temporal anchor — we can only select what is literally inside the
        # polygon; there is no window to bridge GPS-less media into.
        included = sorted(anchors, key=_sort_key)
        unplaceable = [r for r in records if not r.has_gps and r.local_dt is None]
        return TripSelection(included=included, anchors=anchors, unplaceable=unplaceable)

    window = _day_window(anchor_times)
    bridged = [r for r in records if not r.has_gps and _within(r.local_dt, window)]
    included = sorted([*anchors, *bridged], key=_sort_key)
    unplaceable = [r for r in records if not r.has_gps and r.local_dt is None]
    edge_day = _edge_day_paths(included, window)
    return TripSelection(
        included=included,
        anchors=anchors,
        edge_day=edge_day,
        window=window,
        unplaceable=unplaceable,
    )


def select_by_date_range(records: list[TripRecord], start: datetime, end: datetime) -> TripSelection:
    """Select every record whose capture time lies in [start, end], any GPS state.

    ``start`` and ``end`` are naive local datetimes. Records without a capture time
    cannot be placed and are returned in ``unplaceable``.
    """
    window = (start, end)
    included = sorted([r for r in records if _within(r.local_dt, window)], key=_sort_key)
    unplaceable = [r for r in records if r.local_dt is None]
    edge_day = _edge_day_paths(included, window)
    return TripSelection(
        included=included,
        anchors=[r for r in included if r.has_gps],
        edge_day=edge_day,
        window=window,
        unplaceable=unplaceable,
    )


def build_day_buckets(records: list[TripRecord]) -> list[DayBucket]:
    """Return one sorted histogram bucket per day with at least one dated record."""
    buckets: dict[date, DayBucket] = {}
    for record in records:
        if record.local_dt is None:
            continue
        day = record.local_dt.date()
        bucket = buckets.setdefault(day, DayBucket(day=day))
        bucket.total += 1
        if record.kind == "video":
            bucket.videos += 1
        else:
            bucket.images += 1
        if record.has_gps:
            bucket.gps_count += 1
    return [buckets[day] for day in sorted(buckets)]


def select_by_days(records: list[TripRecord], selected_days: set[date]) -> TripSelection:
    """Select records captured on the explicitly selected local dates."""
    included = sorted(
        [
            record
            for record in records
            if record.local_dt is not None and record.local_dt.date() in selected_days
        ],
        key=_sort_key,
    )
    unplaceable = [record for record in records if record.local_dt is None]
    if not selected_days:
        return TripSelection(included=[], anchors=[], unplaceable=unplaceable)
    first_day = min(selected_days)
    last_day = max(selected_days)
    window = (
        datetime.combine(first_day, time.min),
        datetime.combine(last_day, time.max),
    )
    return TripSelection(
        included=included,
        anchors=[record for record in included if record.has_gps],
        edge_day=_edge_day_paths(included, window),
        window=window,
        unplaceable=unplaceable,
    )


def centroid_of(records: list[TripRecord]) -> tuple[float, float] | None:
    """Return the mean (lat, lon) of the GPS-bearing records, or None."""
    points = [(r.lat, r.lon) for r in records if r.has_gps]
    if not points:
        return None
    lat = sum(p[0] for p in points) / len(points)  # type: ignore[misc]
    lon = sum(p[1] for p in points) / len(points)  # type: ignore[misc]
    return (lat, lon)


def default_folder_name(window: tuple[datetime, datetime] | None) -> str:
    """Return a date-based folder name fallback for a selection window."""
    if window is None:
        return "Auswahl"
    start_day = window[0].date()
    end_day = window[1].date()
    if start_day == end_day:
        return start_day.isoformat()
    return f"{start_day.isoformat()}_{end_day.isoformat()}"
