"""Leaflet map page and QWebChannel bridge for the Trip Lasso dialog.

The map relies on QtWebEngine; the import is lazy so the rest of the app keeps
working when ``PyQt6-WebEngine`` is not installed (the caller disables the button).
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

LOGGER = logging.getLogger("vmp.gui.lasso.map_view")

_webengine: object | None = None


def webengine_available() -> bool:
    """Return True if QtWebEngine can be imported (cached, lazy)."""
    global _webengine
    if _webengine is None:
        try:
            from PyQt6.QtWebChannel import QWebChannel
            from PyQt6.QtWebEngineWidgets import QWebEngineView

            _webengine = (QWebEngineView, QWebChannel)
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("QtWebEngine unavailable, Trip Lasso disabled: %s", exc)
            _webengine = False
    return bool(_webengine)


# Markers are keyed by a *stable* id (the record's path string), not by list
# index: indices shift whenever records are removed after a move, which used to
# highlight the wrong points and leave moved markers on the map forever.
MAP_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css"/>
<style>html,body,#map{height:100%;margin:0;padding:0;background:#eef1f5}</style>
</head><body><div id="map"></div>
<script>__QWEBCHANNEL_JS__</script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.js"></script>
<script>
window.onerror = function(msg, src, line, col, err){
  console.log('JS ERROR: ' + msg + ' @' + src + ':' + line + ':' + col);
};
console.log('lasso map script start; Leaflet=' + (typeof L) + ' QWebChannel=' + (typeof QWebChannel));
var map, drawn, markers = {}, bridge = null;
var DEFAULT = {color:'#2f6fed', fillColor:'#2f6fed', fillOpacity:0.7, radius:5, weight:1};
function initMap(pts){
  console.log('initMap with ' + pts.length + ' points; Leaflet=' + (typeof L));
  if (typeof L === 'undefined'){ console.log('Leaflet NOT loaded (CDN blocked?)'); return; }
  map = L.map('map');
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    {maxZoom:19, attribution:'© OpenStreetMap'}).addTo(map);
  var renderer = L.canvas(), bounds = [];
  pts.forEach(function(p){
    var m = L.circleMarker([p.lat, p.lon], DEFAULT);
    m.options.renderer = renderer; m.addTo(map);
    markers[p.id] = m; bounds.push([p.lat, p.lon]);
  });
  if (bounds.length) { map.fitBounds(bounds, {padding:[30,30]}); }
  else { map.setView([20, 0], 2); }
  drawn = new L.FeatureGroup(); map.addLayer(drawn);
  var polyOpts = {allowIntersection:false, shapeOptions:{color:'#e0532f'}};
  var draw = new L.Control.Draw({
    draw: {polygon: polyOpts,
           marker:false, polyline:false, rectangle:false, circle:false, circlemarker:false},
    edit: {featureGroup: drawn, edit: false, remove: true}
  });
  map.addControl(draw);
  map.on(L.Draw.Event.CREATED, function(e){ drawn.clearLayers(); drawn.addLayer(e.layer); sendPolygon(e.layer); });
  map.on(L.Draw.Event.DELETED, function(){ if (bridge) bridge.polygon_drawn('[]'); });
  // Arm the polygon tool right away so the user can start drawing on open.
  try { window._poly = new L.Draw.Polygon(map, polyOpts); window._poly.enable(); console.log('polygon tool armed'); }
  catch (err) { console.log('could not arm polygon tool: ' + err); }
}
function sendPolygon(layer){
  var ll = layer.getLatLngs()[0].map(function(p){ return [p.lat, p.lng]; });
  if (bridge) bridge.polygon_drawn(JSON.stringify(ll));
}
function highlight(ids){
  var inset = {}; ids.forEach(function(i){ inset[i] = true; });
  for (var id in markers){
    var on = inset[id] === true;
    markers[id].setStyle({color: on?'#2f6fed':'#9aa6b2', fillColor: on?'#2f6fed':'#9aa6b2',
      fillOpacity: on?0.9:0.25, radius: on?6:4});
  }
}
function removeMarkers(ids){
  ids.forEach(function(id){
    if (markers[id]){ map.removeLayer(markers[id]); delete markers[id]; }
  });
}
function resetColors(){ for (var id in markers){ markers[id].setStyle(DEFAULT); } }
function connectChannel(){
  if (typeof QWebChannel === 'undefined'){ console.log('QWebChannel undefined; retrying'); return setTimeout(connectChannel, 100); }
  if (typeof qt === 'undefined' || !qt.webChannelTransport){ console.log('qt.webChannelTransport missing; retrying'); return setTimeout(connectChannel, 100); }
  console.log('connecting QWebChannel');
  new QWebChannel(qt.webChannelTransport, function(channel){
    bridge = channel.objects.bridge;
    console.log('bridge connected; requesting points');
    bridge.get_points(function(jsonStr){ initMap(JSON.parse(jsonStr)); });
  });
}
connectChannel();
</script></body></html>
"""


def qwebchannel_js() -> str:
    """Return the bundled qwebchannel.js source from Qt's resource system."""
    from PyQt6.QtCore import QFile, QIODevice

    for resource in (":/qtwebchannel/qwebchannel.js", "qrc:///qtwebchannel/qwebchannel.js"):
        handle = QFile(resource)
        if handle.open(QIODevice.OpenModeFlag.ReadOnly):
            data = bytes(handle.readAll()).decode("utf-8", "replace")
            handle.close()
            return data
    LOGGER.warning("qwebchannel.js resource not found; map bridge will not connect")
    return ""


class MapBridge(QObject):
    """QWebChannel bridge: serves GPS points to JS and receives drawn polygons."""

    polygonReceived = pyqtSignal(str)

    def __init__(self, points_json: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._points_json = points_json

    @pyqtSlot(result=str)
    def get_points(self) -> str:  # noqa: D401 - Qt slot
        """Return the GPS points as a JSON string (pulled by JS on load)."""
        LOGGER.info("Lasso map: JS requested points (%s bytes)", len(self._points_json))
        return self._points_json

    @pyqtSlot(str)
    def polygon_drawn(self, geojson: str) -> None:
        """Receive the polygon vertices (JSON [[lat,lon],...]) from JS."""
        LOGGER.info("Lasso map: polygon received from JS: %s", geojson[:200])
        self.polygonReceived.emit(geojson)
