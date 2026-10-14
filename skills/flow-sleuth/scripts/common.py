"""Shared lookups: cache paths, state of registry from the ICAO address, country polygons, airports, aircraft class."""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import urllib.request
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
CACHE = Path(os.environ.get("FLOW_SLEUTH_CACHE", Path.home() / ".cache" / "flow-sleuth"))
UA = {"User-Agent": "flow-sleuth (https://github.com/mstkyvz/flow-sleuth)"}
AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

# operator names that mark a state aircraft (government, royal or VIP flights, air arms, police) when the
# database has no military flag. Matched against ownOp, case-insensitive.
STATE_RE = re.compile(r"government|governo|gobierno|regierung|royal|amiri|emiri|presiden|ministry|ministr|"
                      r"air force|airforce|navy|naval|army|armée|luftwaffe|military|coast ?guard|police|polis|"
                      r"emniyet|jandarma|gendarm|state of |republic of |kingdom of |sultanate|head of state", re.I)


def get(url: str, timeout: int = 120) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


@lru_cache(None)
def blocks():
    b = json.loads((DATA / "icao_blocks.json").read_text())["blocks"]
    return sorted(((int(x["start"], 16), int(x["end"], 16), x["cc"], x["country"]) for x in b), key=lambda x: x[1] - x[0])


def registry(icao: str) -> tuple[str, str]:
    """(cc, country) of the state the ICAO address block belongs to; ('', '') for non-ICAO (~) addresses."""
    try:
        v = int(icao, 16)
    except (TypeError, ValueError):
        return "", ""
    for a, b, cc, name in blocks():          # smallest block first, so sub-allocations win
        if a <= v <= b:
            return cc, name
    return "", ""


def aircraft_class(db_flags: int | None, own_op: str | None) -> str:
    if (db_flags or 0) & 1:
        return "mil"
    if own_op and STATE_RE.search(own_op):
        return "state"
    return "civil"


def private(db_flags: int | None) -> bool:
    """PIA (privacy ICAO address) or LADD (limited aircraft data displayed): owners asked not to be tracked."""
    return bool((db_flags or 0) & (4 | 8))


class Countries:
    def __init__(self):
        from shapely import STRtree
        from shapely.geometry import shape
        fc = json.loads((DATA / "countries.geojson").read_text())
        self.cc = [f["properties"]["cc"] for f in fc["features"]]
        self.name = {f["properties"]["cc"]: f["properties"]["name"] for f in fc["features"]}
        self.geom = [shape(f["geometry"]) for f in fc["features"]]
        self.tree = STRtree(self.geom)

    def of(self, lons, lats) -> list[str]:
        """Country code for each point ('' = sea or outside every polygon)."""
        import numpy as np
        import shapely
        pts = shapely.points(np.asarray(lons, float), np.asarray(lats, float))
        out = [""] * len(pts)
        ip, ig = self.tree.query(pts, predicate="within")
        for p, g in zip(ip, ig):
            out[p] = self.cc[g]
        return out

    def polygon(self, cc: str):
        from shapely.ops import unary_union
        gs = [g for c, g in zip(self.cc, self.geom) if c == cc.upper()]
        if not gs:
            raise SystemExit(f"unknown country code {cc}; codes are ISO 3166-1 alpha-2 (IL, AZ, SY, ...)")
        return unary_union(gs)


class Airports:
    """OurAirports (public domain): every airport, heliport and airbase with ICAO/IATA codes and country."""
    KINDS = {"large_airport": 0, "medium_airport": 1, "small_airport": 2, "heliport": 3, "seaplane_base": 3, "balloonport": 4}

    def __init__(self):
        import numpy as np
        from scipy.spatial import cKDTree
        f = CACHE / "airports.csv"
        if not f.exists() or f.stat().st_size < 1e6:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(get(AIRPORTS_URL))
        rows = [r for r in csv.DictReader(io.StringIO(f.read_text(encoding="utf-8")))
                if r["type"] in self.KINDS and r["latitude_deg"] and r["longitude_deg"]]
        self.rows = rows
        lat = np.radians([float(r["latitude_deg"]) for r in rows])
        lon = np.radians([float(r["longitude_deg"]) for r in rows])
        self.xyz = np.c_[np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)]
        self.tree = cKDTree(self.xyz)

    def by_code(self, code: str) -> dict | None:
        if not hasattr(self, "_code"):
            self._code = {}
            for r in self.rows:
                for k in (r["gps_code"], r["ident"], r.get("icao_code", "")):
                    if k and k not in self._code:
                        self._code[k] = r
        r = self._code.get(code)
        if not r:
            return None
        return {"code": code, "iata": r["iata_code"] or None, "name": r["name"], "cc": r["iso_country"], "type": r["type"],
                "lat": float(r["latitude_deg"]), "lon": float(r["longitude_deg"])}

    def near(self, lat: float, lon: float, km: float = 6.0, kinds: tuple | None = None) -> dict | None:
        """Nearest airport within km, preferring larger ones when several are close."""
        import numpy as np
        la, lo = math.radians(lat), math.radians(lon)
        q = [math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)]
        idx = self.tree.query_ball_point(q, km / 6371.0)
        if kinds:
            idx = [i for i in idx if self.rows[i]["type"] in kinds]
        if not idx:
            return None
        d = np.linalg.norm(self.xyz[idx] - np.array(q), axis=1) * 6371.0
        best = min(range(len(idx)), key=lambda i: (self.KINDS[self.rows[idx[i]]["type"]] > 2, d[i] > 3, d[i]))
        r = self.rows[idx[best]]
        return {"code": r["gps_code"] or r["ident"], "iata": r["iata_code"] or None, "name": r["name"],
                "cc": r["iso_country"], "type": r["type"], "km": round(float(d[best]), 1),
                "elev_ft": int(float(r["elevation_ft"])) if r["elevation_ft"] else 0}


def day_file(date: str) -> Path:
    return CACHE / "days" / f"{date}.jsonl.gz"


class Routes:
    """Scheduled routes by callsign from VRS standing data (CC0): 'THY373' -> ['LTFM', 'UBBB']. A callsign is a
    timetable entry, so the route is what the flight was planned to fly, not proof of where it went."""
    URL = "https://codeload.github.com/vradarserver/standing-data/zip/refs/heads/main"

    def __init__(self):
        import zipfile
        f = CACHE / "standing-data.zip"
        if not f.exists() or time_age_days(f) > 14:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(get(self.URL, timeout=300))
        self.r = {}
        with zipfile.ZipFile(f) as z:
            for n in z.namelist():
                if "/routes/schema-01/" in n and n.endswith(".csv"):
                    for line in z.read(n).decode("utf-8", "replace").splitlines()[1:]:
                        p = line.split(",")
                        if len(p) >= 5 and p[4]:
                            self.r[p[0]] = p[4].split("-")

    def get(self, callsign: str) -> list[str] | None:
        return self.r.get((callsign or "").strip().upper())


def time_age_days(f: Path) -> float:
    import time
    return (time.time() - f.stat().st_mtime) / 86400
