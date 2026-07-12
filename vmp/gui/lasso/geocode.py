"""Best-effort reverse geocoding (Nominatim/OSM) for trip folder name suggestions."""

from __future__ import annotations

import json
import logging

LOGGER = logging.getLogger("vmp.gui.lasso.geocode")

# Cache by rounded coordinates so repeated suggestions for the same trip area
# reuse one HTTP response (also keeps Nominatim usage polite during drags).
_GEOCODE_CACHE: dict[tuple[float, float], str | None] = {}


def reverse_geocode_place(lat: float, lon: float, timeout: float = 4.0) -> str | None:
    """Reverse geocode to a place label via Nominatim (OSM); None on failure."""
    cache_key = (round(lat, 3), round(lon, 3))
    if cache_key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[cache_key]
    place = _reverse_geocode_uncached(lat, lon, timeout)
    _GEOCODE_CACHE[cache_key] = place
    return place


def _reverse_geocode_uncached(lat: float, lon: float, timeout: float) -> str | None:
    import urllib.parse
    import urllib.request

    params = urllib.parse.urlencode(
        {"format": "jsonv2", "lat": f"{lat:.5f}", "lon": f"{lon:.5f}", "zoom": "10", "addressdetails": "1"}
    )
    url = "https://nominatim.openstreetmap.org/reverse?" + params
    request = urllib.request.Request(url, headers={"User-Agent": "VacationMediaProcessor/0.1 (media organizer)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        LOGGER.debug("Reverse geocode failed for %s,%s", lat, lon, exc_info=True)
        return None
    address = data.get("address", {})
    for key in ("island", "city", "town", "village", "municipality", "county", "state", "country"):
        if address.get(key):
            return str(address[key])
    name = data.get("name")
    return str(name) if name else None
