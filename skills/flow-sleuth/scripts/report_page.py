"""HTML page for relations.py: signals, trade charts, top products, shipping, flights. Chart.js from cdnjs."""
from __future__ import annotations

import html
import json

from mapview import flag


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def usd(v) -> str:
    if v is None:
        return "–"
    v = float(v)
    return f"${v / 1e9:.2f} bn" if abs(v) >= 1e9 else f"${v / 1e6:,.0f} M" if abs(v) >= 1e6 else f"${v:,.0f}"


def _place(a) -> str:
    if not a:
        return "?"
    return f"{'~' if a.get('probable') else '*' if a.get('schedule') else ''}{esc(a['code'])} ({esc(a['cc'])})"


def write(path, rep: dict, has_map: bool) -> None:
    C = rep["countries"]
    nm = lambda c: f"{flag(c)} {esc(C[c]['name'])}"  # noqa: E731
    parts, charts = [], []

    # ---------------- pairs
    for key, p in rep["pairs"].items():
        A, B = p["a"], p["b"]
        t = p["trade"]
        parts.append(f"<section><h2>{nm(A)} ⇄ {nm(B)}</h2>")
        parts.append("<h3>Signals</h3><ul class='sig'>" + "".join(f"<li>{esc(s)}</li>" for s in p["signals"]) + "</ul>")
        cid = f"y_{key}"
        charts.append({"id": cid, "type": "bar", "labels": [r["year"] for r in t["yearly"]], "datasets": [
            {"label": f"{A} → {B} ({A} reports exports)", "data": [r.get("a_X") for r in t["yearly"]], "color": "#d62728"},
            {"label": f"{A} → {B} ({B} reports imports)", "data": [r.get("b_M") for r in t["yearly"]], "color": "#f4a3a3"},
            {"label": f"{B} → {A} ({B} reports exports)", "data": [r.get("b_X") for r in t["yearly"]], "color": "#1f77b4"},
            {"label": f"{B} → {A} ({A} reports imports)", "data": [r.get("a_M") for r in t["yearly"]], "color": "#9ec5e8"}]})
        parts.append(f"<h3>Trade per year (UN Comtrade, USD)</h3><p class='mut'>Each flow as both sides report it. "
                     f"A missing bar means that side reported nothing for the year.</p><div class='ch'><canvas id='{cid}'></canvas></div>")
        if t["monthly"]:
            cid = f"m_{key}"
            charts.append({"id": cid, "type": "line", "labels": [r["month"] for r in t["monthly"]], "datasets": [
                {"label": f"{A} → {B}", "data": [r.get("a_X") or r.get("b_M") for r in t["monthly"]], "color": "#d62728"},
                {"label": f"{B} → {A}", "data": [r.get("b_X") or r.get("a_M") for r in t["monthly"]], "color": "#1f77b4"}]})
            parts.append(f"<h3>Last months</h3><p class='mut'>Exporter's figure, else the importer's. Recent months fill in "
                         f"as countries publish (often 2–4 months late).</p><div class='ch'><canvas id='{cid}'></canvas></div>")
        tops = []
        for k, title in ((f"a_X", f"{A} sells to {B}"), (f"a_M", f"{A} buys from {B}"), (f"b_X", f"{B} sells to {A}"), (f"b_M", f"{B} buys from {A}")):
            x = t["top"].get(k)
            if x and x["items"]:
                tops.append(f"<div><h4>{esc(title)} <span class='mut'>({x['year']})</span></h4><table>" +
                            "".join(f"<tr><td>{esc(i['name'])[:60]}</td><td class='n'>{usd(i['usd'])}</td></tr>" for i in x["items"]) +
                            "</table></div>")
        if tops:
            parts.append("<h3>What is traded (top HS chapters)</h3><div class='grid'>" + "".join(tops) + "</div>")
        parts.append("</section>")

    # ---------------- shipping
    parts.append("<section><h2>Shipping</h2><p class='mut'>IMF PortWatch, from AIS ship positions. Port calls and "
                 "tonnage are measured; the port-to-country trade values are IMF's modelled baseline, not live flows.</p>")
    for c, s in rep["shipping"].items():
        parts.append(f"<h3>{nm(c)}</h3>")
        if s["monthly"]:
            cid = f"s_{c}"
            ms = s["monthly"]
            charts.append({"id": cid, "type": "line", "labels": [r["date"][:7] for r in ms], "y2": True, "datasets": [
                {"label": "port calls (cargo + tanker)", "data": [sum((r.get(k) or 0) for k in ("ais_portcalls_container", "ais_portcalls_tanker",
                 "ais_portcalls_dry_bulk", "ais_portcalls_general_cargo", "ais_portcalls_roro")) for r in ms], "color": "#2ca02c"},
                {"label": "seaborne imports (t)", "data": [r.get("volume_import_total") for r in ms], "color": "#1f77b4", "axis": "y2"},
                {"label": "seaborne exports (t)", "data": [r.get("volume_export_total") for r in ms], "color": "#d62728", "axis": "y2"}]})
            parts.append(f"<div class='ch'><canvas id='{cid}'></canvas></div>")
        if not s["ports_30d"]:
            parts.append("<p class='mut'>PortWatch publishes no daily port figures for this country.</p>")
        if s["ports_30d"]:
            parts.append("<h4>Busiest ports, last 30 days</h4><table><tr><th>port</th><th class='n'>calls</th><th class='n'>tanker calls</th>"
                         "<th class='n'>import t</th><th class='n'>export t</th></tr>" +
                         "".join(f"<tr><td>{esc(r['portname'])}</td><td class='n'>{int(r['portcalls'] or 0)}</td><td class='n'>{int(r['portcalls_tanker'] or 0)}</td>"
                                 f"<td class='n'>{int(r['import'] or 0):,}</td><td class='n'>{int(r['export'] or 0):,}</td></tr>" for r in s["ports_30d"]) + "</table>")
        for o3, links in s["port_links"].items():
            other = next((k for k, v in C.items() if v["iso3"] == o3), o3)
            if links:
                parts.append(f"<h4>Ports of {esc(C[c]['name'])} trading with {nm(other)} <span class='mut'>(modelled, USD per day)</span></h4>"
                             "<table><tr><th>port</th><th class='n'>exports/day</th><th class='n'>imports/day</th><th>main goods</th></tr>" +
                             "".join(f"<tr><td>{esc(x['port'])}</td><td class='n'>{usd(x['export_usd_day'])}</td><td class='n'>{usd(x['import_usd_day'])}</td>"
                                     f"<td class='mut'>{esc(', '.join(x['industries']))}</td></tr>" for x in links) + "</table>")
    parts.append("</section>")

    # ---------------- flights
    fl = rep.get("flights")
    if fl:
        parts.append(f"<section><h2>Flights {esc(fl['span'])}</h2><p class='mut'>ADS-B (adsb.lol). Only aircraft that "
                     "transmit and are heard; many military flights are dark. ~ = probable airport (lost low nearby), "
                     "* = from the callsign's timetable.</p><table>"
                     "<tr><th>between</th><th class='n'>take-off and landing seen</th><th class='n'>an end probable or from timetable</th>"
                     "<th class='n'>of them military/state</th></tr>" +
                     "".join(f"<tr><td>{nm(k.split('-')[0])} → {nm(k.split('-')[1])}</td><td class='n'>{v['direct']}</td>"
                             f"<td class='n'>{v['inferred']}</td><td class='n'>{v['direct_mil_state']}</td></tr>" for k, v in fl["pairs"].items()) + "</table>")
        rows = []
        for k, vs in fl["state_visits"].items():
            a, b = k.split("-in-")
            for v in vs:
                rows.append(f"<tr><td>{nm(a)} in {nm(b)}</td><td>{esc(v['reg'])} <b>{esc(v['type'])}</b> <span class='mut'>{esc(v['desc'])}</span></td>"
                            f"<td>{esc('/'.join(v['cs']))}</td><td class='mut'>{esc(v['op'])}</td><td>{_place(v['from'])} → {_place(v['to'])}</td>"
                            f"<td class='mut'>{esc(' › '.join(v['countries']))}</td></tr>")
        if rows:
            parts.append("<h3>Military and state aircraft of one country in or over the other</h3><table><tr><th></th><th>aircraft</th>"
                         "<th>callsign</th><th>operator</th><th>from → to</th><th>countries</th></tr>" + "".join(rows) + "</table>")
        parts.append("<h3>Overflights</h3><table>" + "".join(
            f"<tr><td>aircraft registered in {nm(k.split('-over-')[0])} over {nm(k.split('-over-')[1])}</td><td class='n'>{v}</td></tr>"
            for k, v in fl["overflights"].items()) + "</table>")
        if has_map:
            parts.append("<p><a class='btn' href='flights.html'>Open the flight map →</a></p>"
                         "<iframe src='flights.html' loading='lazy'></iframe>")
        parts.append("</section>")

    title = " · ".join(C[c]["name"] for c in C)
    page = PAGE.replace("__TITLE__", esc(title)).replace("__BODY__", "\n".join(parts)) \
               .replace("__CHARTS__", json.dumps(charts)).replace("__GEN__", esc(rep["generated"]))
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#fafaf8;--fg:#1d1d1f;--mut:#666;--card:#fff;--line:#e4e4e0;--acc:#1f4fe0}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececf0;--mut:#a5a5ad;--card:#1d1d21;--line:#2e2e34;--acc:#7a9bff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 60px}h1{margin:0 0 4px;font-size:clamp(24px,4vw,34px)}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:18px 0}
h2{margin:0 0 8px;font-size:21px}h3{margin:18px 0 6px;font-size:16px}h4{margin:12px 0 4px;font-size:14px}
.mut{color:var(--mut);font-size:13px}.sig li{margin:3px 0}.ch{position:relative;height:300px}
table{border-collapse:collapse;width:100%;font-size:13.5px;margin:4px 0}td,th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.n{text-align:right;white-space:nowrap}.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
iframe{width:100%;height:620px;border:1px solid var(--line);border-radius:10px}.btn{color:var(--acc);font-weight:600}
@media (max-width:600px){table{font-size:12px}.ch{height:240px}}
</style></head><body><main><h1>__TITLE__</h1><p class="mut">flow-sleuth relations report · generated __GEN__ UTC ·
measurable signals only, no verdict · sources: UN Comtrade, IMF PortWatch, adsb.lol (ODbL)</p>
__BODY__
</main><script>
const fmt=v=>v==null?'–':Math.abs(v)>=1e9?(v/1e9).toFixed(2)+' bn':Math.abs(v)>=1e6?(v/1e6).toFixed(0)+' M':Math.abs(v)>=1e3?(v/1e3).toFixed(0)+' k':v;
for(const c of __CHARTS__){
  const el=document.getElementById(c.id);if(!el)continue;
  new Chart(el,{type:c.type,data:{labels:c.labels,datasets:c.datasets.map(d=>({label:d.label,data:d.data,backgroundColor:d.color,borderColor:d.color,
    yAxisID:d.axis||'y',tension:.25,pointRadius:2,spanGaps:true}))},
    options:{maintainAspectRatio:false,interaction:{mode:'index',intersect:false},plugins:{tooltip:{callbacks:{label:x=>x.dataset.label+': '+fmt(x.raw)}}},
      scales:Object.assign({y:{ticks:{callback:fmt}}},c.y2?{y2:{position:'right',grid:{drawOnChartArea:false},ticks:{callback:fmt}}}:{})}});
}
</script></body></html>"""
