"""Leaflet page and bridge for the missing-GPS repair dialog."""

from __future__ import annotations

import json

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from ..common.map_provider import configure_local_map_settings

__all__ = ["MAP_HTML", "GpsMapBridge", "configure_local_map_settings"]


MAP_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
html,body,#map{height:100%;margin:0;background:#eef1f5}
.leaflet-tooltip.gps-media-tooltip{padding:7px;max-width:300px;border-radius:7px;box-shadow:0 2px 10px #0003}
.gps-media-card{display:flex;gap:8px;align-items:center;min-width:180px}
.gps-media-card+.gps-media-card{border-top:1px solid #dce1e7;margin-top:6px;padding-top:6px}
.gps-media-card img{width:82px;height:62px;object-fit:cover;border-radius:4px;background:#dfe4ea;
  transition:width .15s ease,height .15s ease}
.leaflet-tooltip.gps-media-tooltip.gps-media-expanded{max-width:430px}
.gps-media-expanded .gps-media-card{min-width:300px}
.gps-media-expanded .gps-media-card img{width:176px;height:132px}
.gps-media-name{font:600 12px/1.25 sans-serif;overflow-wrap:anywhere}
.gps-media-role{font:11px/1.3 sans-serif;color:#59636e;margin-top:3px}
.gps-anchor-icon{background:transparent;border:0}
.gps-anchor-dot{width:18px;height:18px;border:2px solid #fff;border-radius:50%;box-shadow:0 1px 5px #0007;
  display:flex;align-items:center;justify-content:center;color:#fff;font:700 10px/1 sans-serif;box-sizing:border-box}
.gps-anchor-dot.used{background:#e0532f}.gps-anchor-dot.context{background:#2f6fed}
</style>
</head><body><div id="map"></div>
<script>__QWEBCHANNEL_JS__</script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
window.onerror=function(message,source,line,column){
  console.error('GPS map JS error: '+message+' @'+source+':'+line+':'+column);
  report('javascript_error');
};
var map=null, bridge=null, contextLayer=null, pin=null, pinMeta=null;
__MAP_PROVIDER_JS__
function report(s){console.log('GPS map status: '+s);if(bridge){bridge.map_status(s);} }
function escapeHtml(value){
  return String(value||'').replace(/[&<>"']/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
function mediaCard(item){
  var image=item.thumbnail?'<img src="'+item.thumbnail+'" alt="">':'';
  return '<div class="gps-media-card">'+image+'<div><div class="gps-media-name">'+
    escapeHtml(item.label||'GPS')+'</div><div class="gps-media-role">'+escapeHtml(item.role||'')+'</div></div></div>';
}
var ANCHOR_PREVIEW_DELAY_MS=700;
function bindExpandableAnchorTooltip(marker,content){
  var expandTimer=null;
  marker.bindTooltip(content,{direction:'top',className:'gps-media-tooltip',opacity:.98});
  function setExpanded(expanded){
    var tooltip=marker.getTooltip(),element=tooltip&&tooltip.getElement();
    if(element){element.classList.toggle('gps-media-expanded',expanded);}
  }
  function resetPreview(){
    if(expandTimer!==null){clearTimeout(expandTimer);expandTimer=null;}
    setExpanded(false);
  }
  marker.on('mouseover',function(){
    resetPreview();
    expandTimer=setTimeout(function(){expandTimer=null;setExpanded(true);},ANCHOR_PREVIEW_DELAY_MS);
  });
  marker.on('mouseout',resetPreview);
  marker.on('remove',resetPreview);
}
function setPin(lat,lon,meta){
  if(!map){return;}
  pinMeta=meta||pinMeta||{};
  if(!pin){
    pin=L.marker([lat,lon],{draggable:true}).addTo(map);
    pin.on('dragend',function(){var p=pin.getLatLng();bridge.pin_moved(p.lat,p.lng);});
  } else {pin.setLatLng([lat,lon]);}
  var content=mediaCard(pinMeta);
  if(pin.getTooltip()){pin.setTooltipContent(content);}
  else{pin.bindTooltip(content,{direction:'top',className:'gps-media-tooltip',opacity:.98});}
}
function render(raw){
  if(!map){return;}
  var data=(typeof raw==='string')?JSON.parse(raw):raw;
  if(contextLayer){contextLayer.clearLayers();} else {contextLayer=L.layerGroup().addTo(map);}
  var bounds=[],groups={};
  (data.context||[]).forEach(function(p){
    var key=Number(p.lat).toFixed(6)+','+Number(p.lon).toFixed(6);
    if(!groups[key]){groups[key]={lat:p.lat,lon:p.lon,immediate:false,items:[]};}
    groups[key].immediate=groups[key].immediate||p.immediate;
    groups[key].items.push(p);
  });
  Object.keys(groups).forEach(function(key){
    var group=groups[key],count=group.items.length;
    var dot='<div class="gps-anchor-dot '+(group.immediate?'used':'context')+'">'+(count>1?count:'')+'</div>';
    var marker=L.marker([group.lat,group.lon],{icon:L.divIcon({className:'gps-anchor-icon',html:dot,iconSize:[18,18],iconAnchor:[9,9]})});
    bindExpandableAnchorTooltip(marker,group.items.map(mediaCard).join(''));
    marker.addTo(contextLayer);bounds.push([group.lat,group.lon]);
  });
  if(data.pin){setPin(data.pin.lat,data.pin.lon,data.pin);bounds.push([data.pin.lat,data.pin.lon]);}
  else if(pin){map.removeLayer(pin);pin=null;}
  if(bounds.length===1){map.setView(bounds[0],16);}
  else if(bounds.length){map.fitBounds(bounds,{padding:[35,35],maxZoom:17});}
  else {map.setView([20,0],2);}
}
function init(){
  if(typeof L==='undefined'){report('leaflet_missing');return;}
  map=L.map('map');
  setTileProvider(initialTileProvider);
  map.on('click',function(e){setPin(e.latlng.lat,e.latlng.lng,pinMeta);bridge.pin_moved(e.latlng.lat,e.latlng.lng);});
  bridge.get_payload(function(raw){render(raw);report('ready');});
}
function connect(){
  if(typeof QWebChannel==='undefined'||typeof qt==='undefined'||!qt.webChannelTransport){return setTimeout(connect,100);}
  new QWebChannel(qt.webChannelTransport,function(channel){
    bridge=channel.objects.gpsBridge;
    if(!bridge){console.error('GPS map bridge is missing');return;}
    init();
  });
}
connect();
</script></body></html>"""


class GpsMapBridge(QObject):
    """Expose map payload and relay manual pin moves."""

    pinMoved = pyqtSignal(float, float)
    statusChanged = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._payload = "{}"

    def set_payload(self, payload: dict) -> None:
        self._payload = json.dumps(payload)

    @pyqtSlot(result=str)
    def get_payload(self) -> str:
        return self._payload

    @pyqtSlot(float, float)
    def pin_moved(self, latitude: float, longitude: float) -> None:
        self.pinMoved.emit(latitude, longitude)

    @pyqtSlot(str)
    def map_status(self, status: str) -> None:
        self.statusChanged.emit(status)
