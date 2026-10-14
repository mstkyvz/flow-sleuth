"""Interactive flight map: one colour per aircraft, a list of flights beside the map (registration, type name,
ICAO type code, callsign, operator, state of registry, from -> to, times, countries crossed), labels on the
routes, click a flight to highlight and zoom to it. Self-contained HTML (Leaflet from cdnjs, OSM tiles)."""
from __future__ import annotations

import datetime as dt
import html
import json

PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4", "#f032e6", "#9a6324", "#469990",
           "#800000", "#808000", "#000075", "#e6ab02", "#1b9e77", "#d95f02", "#7570b3", "#66a61e", "#a6761d"]
CLASS_NAME = {"mil": "military", "state": "state", "civil": "civil"}


def flag(cc: str) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc.upper()) if cc and len(cc) == 2 and cc.isalpha() else ""


def _t(t: int) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%d %b %H:%MZ")


def _end(a, p) -> dict:
    if a:
        return {"code": a["code"], "name": a["name"], "cc": a["cc"], "probable": bool(a.get("probable")),
                "schedule": bool(a.get("schedule"))}
    alt = "ground" if p[3] == -1 else (f"{p[3]} ft" if p[3] is not None else "?")
    return {"code": None, "name": f"lost at {p[1]:.2f}, {p[2]:.2f} ({alt})", "cc": "", "probable": False}


def write(path: str, legs: list[dict], title: str, path_ccs, names: dict | None = None) -> None:
    names = names or {}
    colors, flights = {}, []
    for r in sorted(legs, key=lambda r: r["t0"]):
        c = colors.setdefault(r["icao"], PALETTE[len(colors) % len(PALETTE)])
        flights.append({
            "id": len(flights), "icao": r["icao"], "color": c, "cls": r["cls"],
            "reg": r["r"] or r["icao"].upper(), "type": r["t"] or "", "desc": r["desc"] or "", "op": r["own"] or "",
            "year": r.get("year") or "", "cs": r["cs"], "rcc": r["reg_cc"] or "",
            "rname": names.get(r["reg_cc"], r["reg_cc"] or "unknown"), "flag": flag(r["reg_cc"]),
            "t0": _t(r["t0"]), "t1": _t(r["t1"]),
            "from": _end(r["dep"], r["pts"][0]), "to": _end(r["arr"], r["pts"][-1]),
            "via": path_ccs(r), "p": [[p[1], p[2]] for p in r["pts"]],
        })
    doc = TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__DATA__", json.dumps(flights, ensure_ascii=False))
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


TEMPLATE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
:root{--bg:#fff;--fg:#1d1d1f;--mut:#666;--line:#e3e3e3;--sel:#f3f6ff}
@media (prefers-color-scheme:dark){:root{--bg:#1b1b1e;--fg:#ececf0;--mut:#a0a0a8;--line:#333;--sel:#262b3a}}
*{box-sizing:border-box}html,body{height:100%;margin:0;background:var(--bg);color:var(--fg);font:13px/1.4 system-ui,-apple-system,sans-serif}
#wrap{display:flex;height:100%}#side{width:380px;max-width:42vw;overflow:auto;border-right:1px solid var(--line)}#m{flex:1}
h1{font-size:15px;margin:12px 14px 2px}.sub{color:var(--mut);margin:0 14px 8px;font-size:12px}
.f{display:flex;gap:8px;flex-wrap:wrap;margin:0 14px 8px}.f label{font-size:12px;color:var(--mut)}
.it{padding:9px 14px;border-top:1px solid var(--line);cursor:pointer}.it:hover,.it.on{background:var(--sel)}
.it .h{display:flex;align-items:center;gap:6px;font-weight:600}.sw{width:12px;height:12px;border-radius:3px;flex:none}
.tag{font-size:10.5px;font-weight:600;padding:1px 5px;border-radius:4px;background:#0001;color:var(--mut)}
.tag.mil{background:#d6272822;color:#d62728}.tag.state{background:#9467bd22;color:#9467bd}
.ty{color:var(--fg)}.mu{color:var(--mut);font-size:12px}.rt{margin-top:3px}.rt b{font-weight:600}
.lbl{background:#fff;border:1px solid #0003;border-radius:4px;padding:0 4px;font:600 11px system-ui;white-space:nowrap;color:#222;box-shadow:none}
.lbl:before{display:none}
@media (max-width:760px){#wrap{flex-direction:column-reverse}#side{width:auto;max-width:none;height:45%;border-right:0;border-top:1px solid var(--line)}#m{height:55%}}
</style></head><body><div id="wrap"><div id="side"><h1>__TITLE__</h1>
<p class="sub"><span id="cnt"></span> · ADS-B: adsb.lol (ODbL) · ~ = probable airport (lost low nearby) · * = from the callsign's timetable</p>
<div class="f"><label><input type="checkbox" id="lab" checked> labels on map</label>
<label><input type="checkbox" class="cl" value="mil" checked> military</label>
<label><input type="checkbox" class="cl" value="state" checked> state</label>
<label><input type="checkbox" class="cl" value="civil" checked> civil</label></div><div id="list"></div></div><div id="m"></div></div>
<script>
const D=__DATA__;
const m=L.map('m',{worldCopyJump:true});
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:13,attribution:'© OpenStreetMap contributors'}).addTo(m);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const end=e=>e.code?`${e.probable?'~':e.schedule?'*':''}<b>${esc(e.code)}</b> ${esc(e.name)} (${esc(e.cc)})`:`<i>${esc(e.name)}</i>`;
const all=L.latLngBounds([]);const layers={};let sel=null;
for(const f of D){
  const line=L.polyline(f.p,{color:f.color,weight:2.5,opacity:.85});
  const a=L.circleMarker(f.p[0],{radius:4,color:f.color,fillColor:'#fff',fillOpacity:1,weight:2});
  const b=L.circleMarker(f.p[f.p.length-1],{radius:5,color:f.color,fillColor:f.color,fillOpacity:1});
  const mid=f.p[Math.floor(f.p.length/2)];
  const lab=L.tooltip({permanent:true,direction:'center',className:'lbl',interactive:false}).setLatLng(mid)
     .setContent(`${esc(f.reg)} · ${esc(f.type)}`);
  const pop=`<b>${f.flag} ${esc(f.reg)}</b> — ${esc(f.desc||f.type)} (${esc(f.type)})<br>`+
    `callsign ${esc(f.cs.join(' / ')||'-')} · ${esc(f.op||'operator unknown')}<br>`+
    `registered in ${esc(f.rname)} · <span class="tag ${f.cls}">${f.cls}</span><br>`+
    `${f.t0} ${end(f.from)}<br>${f.t1} → ${end(f.to)}<br>countries: ${esc(f.via.join(' › '))}`;
  const g=L.featureGroup([line,a,b]).bindPopup(pop);
  g.on('click',()=>pick(f.id,false));
  layers[f.id]={g,line,lab,f};all.extend(line.getBounds());
}
function visible(f){return [...document.querySelectorAll('.cl')].some(c=>c.checked&&c.value===f.cls)}
function draw(){const showLab=document.getElementById('lab').checked;let n=0;
  for(const k in layers){const L_=layers[k];const v=visible(L_.f);
    if(v){L_.g.addTo(m);n++;if(showLab)L_.lab.addTo(m);else L_.lab.remove();}else{L_.g.remove();L_.lab.remove();}}
  document.getElementById('cnt').textContent=`${n} flights, ${new Set(D.filter(visible).map(f=>f.icao)).size} aircraft`;
  document.querySelectorAll('.it').forEach(e=>e.style.display=visible(D[e.dataset.id])?'':'none');}
function pick(id,zoom){sel=id;
  for(const k in layers){layers[k].line.setStyle({weight:+k===id?5:2.5,opacity:+k===id?.95:.2});}
  document.querySelectorAll('.it').forEach(e=>e.classList.toggle('on',+e.dataset.id===id));
  const L_=layers[id];L_.g.bringToFront();if(zoom){m.fitBounds(L_.line.getBounds(),{padding:[40,40]});L_.g.openPopup(L_.f.p[Math.floor(L_.f.p.length/2)]);}
  document.querySelector(`.it[data-id="${id}"]`)?.scrollIntoView({block:'nearest'});}
const list=document.getElementById('list');
list.innerHTML=D.map(f=>`<div class="it" data-id="${f.id}"><div class="h"><span class="sw" style="background:${f.color}"></span>`+
  `${f.flag} ${esc(f.reg)} <span class="ty">${esc(f.type)}</span> <span class="tag ${f.cls}">${f.cls}</span></div>`+
  `<div class="mu">${esc(f.desc)}${f.op?' · '+esc(f.op):''}${f.cs.length?' · '+esc(f.cs.join('/')):''}</div>`+
  `<div class="rt">${f.t0} ${end(f.from)}<br>${f.t1} → ${end(f.to)}</div><div class="mu">${esc(f.via.join(' › '))}</div></div>`).join('');
list.querySelectorAll('.it').forEach(e=>e.onclick=()=>pick(+e.dataset.id,true));
document.querySelectorAll('.cl,#lab').forEach(c=>c.onchange=draw);
m.on('popupclose',()=>{sel=null;for(const k in layers)layers[k].line.setStyle({weight:2.5,opacity:.85});document.querySelectorAll('.it').forEach(e=>e.classList.remove('on'))});
if(all.isValid())m.fitBounds(all,{padding:[24,24]});else m.setView([39,35],5);
draw();
</script></body></html>"""
