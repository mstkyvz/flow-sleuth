# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "shapely>=2"]
# ///
"""Live traffic map: aircraft (anywhere), ships and trains (where open live data exists). Runs a small local
server, opens the page in the browser.

  live.py                              # world military + state aircraft, Finnish ships and trains
  live.py --at 32.5,34.9 --nm 250      # all aircraft within 250 NM of the Israeli coast (plus military worldwide)
  live.py --snapshot out/live.json     # write one snapshot (for a static site / GitHub Action) and exit

Sources (no key):
  aircraft  adsb.lol API (ODbL): /v2/mil (aircraft the database marks military) and /v2/point; adsb.fi open data
            (non-commercial use) when adsb.lol is down. Aircraft whose owners asked not to be tracked (PIA / LADD)
            are left out.
  ships     Fintraffic Digitraffic AIS (CC BY 4.0): every vessel in the Finnish and northern Baltic area.
  trains    Fintraffic Digitraffic rail (CC BY 4.0): every train in Finland with GPS position, passenger and freight.
Most countries publish no open live feed for ships or trains; other ship-position sources need an account
(AISStream, Global Fishing Watch) and are not used.
"""
from __future__ import annotations

import argparse
import gzip
import json
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UA = {"User-Agent": "flow-sleuth (https://github.com/mstkyvz/flow-sleuth)", "Accept-Encoding": "gzip"}
PAGE = Path(__file__).resolve().parent.parent / "web" / "live.html"
CACHE: dict[str, tuple[float, object]] = {}
LOCK = threading.Lock()
TRAILS: dict[str, list] = {}          # hex -> [[t, lat, lon], ...] seen while this server runs
TRAIL_S = 2 * 3600
_RT = _AP = None


def route_for(cs: str, lat: float, lon: float) -> dict | None:
    """Origin and destination from the callsign's timetable (VRS standing data); for multi-stop routes the
    segment the aircraft is on now (smallest detour via its position)."""
    global _RT, _AP
    if not cs:
        return None
    if _RT is None:
        try:
            from common import Airports, Routes
            _AP, _RT = Airports(), Routes()
        except Exception as e:  # noqa: BLE001
            print("routes unavailable:", e)
            _RT = False
    if not _RT:
        return None
    r = _RT.get(cs)
    if not r or len(r) < 2:
        return None
    import math

    def km(a, b):
        la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
        h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
        return 12742 * math.asin(min(1, math.sqrt(h)))
    best = None
    for x, y in zip(r, r[1:]):
        X, Y = _AP.by_code(x), _AP.by_code(y)
        if not X or not Y:
            continue
        d = km((X["lat"], X["lon"]), (lat, lon)) + km((lat, lon), (Y["lat"], Y["lon"])) - km((X["lat"], X["lon"]), (Y["lat"], Y["lon"]))
        if best is None or d < best[0]:
            best = (d, X, Y)
    if not best or best[0] > 1500:            # far off the timetable route: callsign reused or data wrong
        return None
    keep = ("code", "iata", "name", "cc", "lat", "lon")
    return {"from": {k: best[1][k] for k in keep}, "to": {k: best[2][k] for k in keep}, "stops": r}

SHIP_KIND = {range(20, 30): "wing in ground", range(30, 31): "fishing", range(31, 33): "towing", range(35, 36): "military",
             range(36, 38): "sailing / pleasure", range(50, 51): "pilot", range(51, 52): "search and rescue", range(52, 53): "tug",
             range(55, 56): "law enforcement", range(60, 70): "passenger", range(70, 80): "cargo", range(80, 90): "tanker"}


def fetch(url: str, ttl: float):
    with LOCK:
        hit = CACHE.get(url)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    d = json.loads(raw)
    with LOCK:
        CACHE[url] = (time.time(), d)
    return d


def ship_kind(t) -> str:
    for rg, k in SHIP_KIND.items():
        if isinstance(t, int) and t in rg:
            return k
    return "other"


def aircraft(at: str | None, nm: int) -> list[dict]:
    out = {}
    # adsb.lol first; adsb.fi (same readsb JSON) when it is down. One source per query, never both.
    la, lo = at.split(",") if at else (None, None)
    queries = [("https://api.adsb.lol/v2/mil", "https://opendata.adsb.fi/api/v2/mil")]
    if at:
        queries.append((f"https://api.adsb.lol/v2/point/{la}/{lo}/{min(nm, 250)}",
                        f"https://opendata.adsb.fi/api/v2/lat/{la}/lon/{lo}/dist/{min(nm, 250)}"))
    for alts in queries:
        for u in alts:
            try:
                d = fetch(u, 8)
            except Exception as e:  # noqa: BLE001
                print("aircraft:", u, e)
                continue
            for a in d.get("ac", d.get("aircraft", [])):
                if a.get("lat") is None or (a.get("dbFlags") or 0) & 12:
                    continue
                cls = "mil" if (a.get("dbFlags") or 0) & 1 else "civil"
                out[a["hex"]] = {"id": a["hex"], "lat": a["lat"], "lon": a["lon"], "trk": a.get("track") or a.get("true_heading") or 0,
                                 "alt": a.get("alt_baro"), "gs": a.get("gs"), "cs": (a.get("flight") or "").strip(),
                                 "reg": a.get("r"), "type": a.get("t"), "desc": a.get("desc"), "op": a.get("ownOp"), "cls": cls,
                                 "sq": a.get("squawk"), "src": "adsb.lol" if "adsb.lol" in u else "adsb.fi"}
                tr = TRAILS.setdefault(a["hex"], [])
                if not tr or (tr[-1][1], tr[-1][2]) != (a["lat"], a["lon"]):
                    tr.append([int(time.time()), round(a["lat"], 4), round(a["lon"], 4)])
            break
    now = time.time()
    for h in list(TRAILS):
        TRAILS[h] = [p for p in TRAILS[h] if now - p[0] < TRAIL_S]
        if not TRAILS[h]:
            del TRAILS[h]
    for x in out.values():
        x["route"] = route_for(x["cs"], x["lat"], x["lon"])
        x["trail"] = [p[1:] for p in TRAILS.get(x["id"], [])] if x["cls"] == "mil" else None
    return list(out.values())


def trail(h: str) -> list:
    """The aircraft's track since its last take-off: today's adsb.lol trace (cut at the last new-leg flag),
    else what this server has seen."""
    try:
        d = fetch(f"https://adsb.lol/data/traces/{h[-2:]}/trace_full_{h}.json", 20)
        pts = d.get("trace") or []
        start = max([i for i, q in enumerate(pts) if (q[6] or 0) & 2] or [0])
        last = d["timestamp"] + (pts[-1][0] if pts else 0)
        # the published trace lags a few minutes: add the positions this server saw after it
        return [[round(q[1], 4), round(q[2], 4)] for q in pts[start:] if q[1] is not None] + \
               [p[1:] for p in TRAILS.get(h, []) if p[0] > last]
    except Exception:  # noqa: BLE001
        return [p[1:] for p in TRAILS.get(h, [])]


def ships() -> list[dict]:
    try:
        loc = fetch("https://meri.digitraffic.fi/api/ais/v1/locations", 30)["features"]
        meta = {v["mmsi"]: v for v in fetch("https://meri.digitraffic.fi/api/ais/v1/vessels", 900)}
    except Exception as e:  # noqa: BLE001
        print("ships:", e)
        return []
    now = time.time() * 1000
    out = []
    for f in loc:
        p = f["properties"]
        if now - p.get("timestampExternal", 0) > 30 * 60 * 1000:
            continue
        m = meta.get(p["mmsi"], {})
        out.append({"id": p["mmsi"], "lon": f["geometry"]["coordinates"][0], "lat": f["geometry"]["coordinates"][1],
                    "trk": p.get("cog") or 0, "sog": p.get("sog"), "name": (m.get("name") or "").strip(), "imo": m.get("imo") or None,
                    "dest": (m.get("destination") or "").strip(), "kind": ship_kind(m.get("shipType")), "moving": (p.get("sog") or 0) > 0.5})
    return out


def trains() -> list[dict]:
    try:
        loc = fetch("https://rata.digitraffic.fi/api/v1/train-locations/latest/", 10)
        meta = {t["trainNumber"]: t for t in fetch("https://rata.digitraffic.fi/api/v1/live-trains?version=0", 300)}
    except Exception as e:  # noqa: BLE001
        print("trains:", e)
        return []
    out = []
    for t in loc:
        m = meta.get(t["trainNumber"], {})
        rows = m.get("timeTableRows") or []
        out.append({"id": t["trainNumber"], "lon": t["location"]["coordinates"][0], "lat": t["location"]["coordinates"][1],
                    "speed": t.get("speed"), "cat": m.get("trainCategory", "?"), "type": m.get("trainType", ""),
                    "op": m.get("operatorShortCode", ""), "line": m.get("commuterLineID") or "",
                    "from": rows[0]["stationShortCode"] if rows else "", "to": rows[-1]["stationShortCode"] if rows else ""})
    return out


def snapshot(at, nm) -> dict:
    return {"t": int(time.time()), "aircraft": aircraft(at, nm), "ships": ships(), "trains": trains()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", help="lat,lon: also all aircraft around this point")
    ap.add_argument("--nm", type=int, default=250)
    ap.add_argument("--port", type=int, default=0, help="default: a free port")
    ap.add_argument("--snapshot", help="write one JSON snapshot and exit")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if a.snapshot:
        s = snapshot(a.at, a.nm)
        Path(a.snapshot).parent.mkdir(parents=True, exist_ok=True)
        Path(a.snapshot).write_text(json.dumps(s, separators=(",", ":")))
        print(f"{a.snapshot}: {len(s['aircraft'])} aircraft, {len(s['ships'])} ships, {len(s['trains'])} trains")
        return

    class H(BaseHTTPRequestHandler):
        def log_message(self, *x):
            pass

        def send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = self.path.split("?")[0]
            if p in ("/", "/live.html"):
                self.send(PAGE.read_bytes(), "text/html; charset=utf-8")
            elif p == "/api/aircraft":
                self.send(json.dumps({"t": int(time.time()), "aircraft": aircraft(a.at, a.nm)}).encode(), "application/json")
            elif p == "/api/trail":
                h = "".join(c for c in self.path.split("hex=")[-1][:6].lower() if c in "0123456789abcdef")
                self.send(json.dumps(trail(h)).encode(), "application/json")
            elif p == "/api/ships":
                self.send(json.dumps({"t": int(time.time()), "ships": ships()}).encode(), "application/json")
            elif p == "/api/trains":
                self.send(json.dumps({"t": int(time.time()), "trains": trains()}).encode(), "application/json")
            else:
                self.send_error(404)

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    a.port = srv.server_address[1]
    url = f"http://127.0.0.1:{a.port}/" + (f"?at={a.at}" if a.at else "")
    print(f"live map: {url}  (Ctrl-C to stop)", flush=True)
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
