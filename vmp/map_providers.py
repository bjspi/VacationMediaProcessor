"""Central definitions and Leaflet configuration for map tile providers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .core.models import MapSettings


@dataclass(frozen=True, slots=True)
class MapProvider:
    """One selectable raster tile provider."""

    id: str
    name: str
    url: str
    max_zoom: int
    attribution: str
    requires_api_key: bool = False


MAP_PROVIDERS: tuple[MapProvider, ...] = (
    MapProvider(
        id="osm",
        name="OpenStreetMap",
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        max_zoom=19,
        attribution=(
            '&copy; <a href="https://www.openstreetmap.org/copyright">'
            "OpenStreetMap contributors</a>"
        ),
    ),
    MapProvider(
        id="opentopo",
        name="OpenTopoMap",
        url="https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        max_zoom=17,
        attribution=(
            'Map data: &copy; <a href="https://www.openstreetmap.org/copyright">'
            'OpenStreetMap contributors</a>, SRTM | Map style: &copy; '
            '<a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)'
        ),
    ),
    MapProvider(
        id="mapy",
        name="Mapy.com",
        url="https://api.mapy.com/v1/maptiles/basic/256/{z}/{x}/{y}?apikey={api_key}",
        max_zoom=20,
        attribution=(
            '&copy; <a href="https://mapy.com/">Mapy.com</a> | '
            '&copy; <a href="https://www.openstreetmap.org/copyright">'
            "OpenStreetMap contributors</a>"
        ),
        requires_api_key=True,
    ),
    MapProvider(
        id="mapy_aerial",
        name="Mapy.com Satellit",
        url="https://api.mapy.com/v1/maptiles/aerial/256/{z}/{x}/{y}?apikey={api_key}",
        max_zoom=20,
        attribution=(
            '&copy; <a href="https://mapy.com/">Mapy.com</a> | '
            '&copy; <a href="https://www.openstreetmap.org/copyright">'
            "OpenStreetMap contributors</a>"
        ),
        requires_api_key=True,
    ),
)

_PROVIDERS_BY_ID = {provider.id: provider for provider in MAP_PROVIDERS}


def normalize_provider_id(value: object) -> str:
    """Return a supported provider id, falling back to OpenStreetMap."""
    provider_id = str(value or "osm").strip().lower()
    return provider_id if provider_id in _PROVIDERS_BY_ID else "osm"


def provider_by_id(provider_id: object) -> MapProvider:
    """Return a provider definition with a safe OSM fallback."""
    return _PROVIDERS_BY_ID[normalize_provider_id(provider_id)]


def provider_configuration_error(provider_id: object, mapy_api_key: str) -> str | None:
    """Return a user-facing validation key for an incomplete provider setup."""
    provider = provider_by_id(provider_id)
    if provider.requires_api_key and not mapy_api_key.strip():
        return "mapy_api_key_missing"
    return None


def provider_requires_api_key(provider_id: object) -> bool:
    """Return whether the selected provider needs the configured Mapy key."""
    return provider_by_id(provider_id).requires_api_key


def leaflet_provider_config(settings: MapSettings) -> dict[str, Any]:
    """Build the Leaflet tile-layer configuration for the selected provider."""
    provider = provider_by_id(settings.provider)
    if provider.requires_api_key and not settings.mapy_api_key.strip():
        provider = provider_by_id("osm")
    url = provider.url
    if provider.requires_api_key:
        url = url.format(api_key=quote(settings.mapy_api_key.strip(), safe=""), z="{z}", x="{x}", y="{y}")
    return {
        "id": provider.id,
        "name": provider.name,
        "url": url,
        "maxZoom": provider.max_zoom,
        "attribution": provider.attribution,
    }


_LEAFLET_PROVIDER_RUNTIME = """
var tileLayer = null;
function setTileProvider(config){
  if(!map || !config){return;}
  if(tileLayer){map.removeLayer(tileLayer);}
  tileLayer=L.tileLayer(config.url,{maxZoom:config.maxZoom,attribution:config.attribution});
  tileLayer.addTo(map);
}
""".strip()


def leaflet_provider_script(settings: MapSettings) -> str:
    """Return shared JS plus the initial provider config for a Leaflet page."""
    config = json.dumps(leaflet_provider_config(settings), ensure_ascii=False)
    return f"var initialTileProvider={config};\n{_LEAFLET_PROVIDER_RUNTIME}"


def leaflet_provider_switch_script(settings: MapSettings) -> str:
    """Return the small JS call used to switch an already loaded Leaflet map."""
    config = json.dumps(leaflet_provider_config(settings), ensure_ascii=False)
    return f"if(typeof setTileProvider==='function'){{setTileProvider({config});}}"
