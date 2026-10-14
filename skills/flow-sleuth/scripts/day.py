# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "shapely>=2"]
# ///
"""Download one day of open ADS-B history and turn it into flights.

  day.py 2026-10-12                 # -> ~/.cache/flow-sleuth/days/2026-10-12.jsonl.gz
  day.py 2026-10-10 2026-10-12      # a range, one file per day
  day.py 2026-10-12 --tar-dir dl/   # from already downloaded .tar.aa/.tar.ab parts

Source (no key): adsb.lol globe_history, one GitHub release per day (ODbL 1.0), the full readsb trace of every
aircraft its receivers heard that day (https://github.com/adsblol/globe_history_2026). About 4 GB a day; it is
streamed, never stored whole.

Each output line is one flight leg: aircraft (ICAO address, registration, type, operator from the readsb database),
state of registry from the ICAO address block, class (mil / state / civil), callsign(s), first and last position,
departure and arrival airport when the leg starts or ends on or near the ground at an airport, the countries the
track crossed in order with times, and a simplified track (a point a minute, plus turns and ground transitions).

Aircraft whose owners asked not to be tracked (PIA and LADD flags in the database) are dropped and only counted.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import subprocess
import sys
import tarfile
import time

from common import Airports, Countries, aircraft_class, day_file, private, registry

AGL_FT = 2500          # a leg end below this height above the airport, and within 6 km of it, is a take-off / landing
GAP_S = 2 * 3600       # positions this far apart start a new leg even without readsb's leg flag


def releases(date: dt.date) -> list[str]:
    """Download URLs of the day's tar parts (prod, else staging)."""
    repo = f"adsblol/globe_history_{date.year}"
    tag = date.strftime("v%Y.%m.%d")
    out = {}
    for kind in ("prod", "staging"):
        t = f"{tag}-planes-readsb-{kind}-0"
        try:
            r = subprocess.run(["gh", "api", f"repos/{repo}/releases/tags/{t}", "-q",
                                ".assets[] | \"\\(.size) \\(.browser_download_url)\""], capture_output=True, text=True, timeout=60)
            lines = r.stdout.split() if r.returncode == 0 else []
        except FileNotFoundError:                 # no gh: try the public API directly
            from common import get
            try:
                lines = [x for a in json.loads(get(f"https://api.github.com/repos/{repo}/releases/tags/{t}"))["assets"]
                         for x in (str(a["size"]), a["browser_download_url"])]
            except Exception:
                lines = []
        parts = sorted(zip(lines[1::2], map(int, lines[0::2])))
        if parts:
            out[kind] = parts
    if not out:
        raise SystemExit(f"no adsb.lol release for {date} in {repo} (data starts 2023; today's day appears the next morning UTC)")
    # use prod unless staging is clearly bigger (prod was down part of the day)
    size = {k: sum(s for _, s in v) for k, v in out.items()}
    best = "prod" if "prod" in out and size["prod"] >= 0.8 * max(size.values()) else max(size, key=size.get)
    return [u for u, _ in out[best]]


def stream(urls: list[str]):
    """The concatenated tar parts as one byte stream (curl, so no Python HTTP buffering limits)."""
    cmd = " ; ".join(f"curl -sfL --retry 3 '{u}'" for u in urls)
    return subprocess.Popen(["sh", "-c", cmd], stdout=subprocess.PIPE, bufsize=1 << 20)


def legs(d: dict):
    """Split a readsb trace into legs; yield lists of points [t, lat, lon, alt|'ground', gs, track, flags, ..., ac]."""
    base = d["timestamp"]
    cur, last_t = [], None
    for p in d.get("trace") or []:
        if p[1] is None or p[2] is None:
            continue
        t = base + p[0]
        if cur and ((p[6] or 0) & 2 or t - last_t > GAP_S):
            yield cur
            cur = []
        cur.append([t] + list(p[1:]))
        last_t = t
    if cur:
        yield cur


def simplify(pts: list) -> list:
    """A point a minute, plus ground/air transitions and turns over 20 degrees."""
    out, lt, ltr, lg = [], -1e9, None, None
    for i, p in enumerate(pts):
        g = p[3] == "ground"
        tr = p[5]
        turn = tr is not None and ltr is not None and abs((tr - ltr + 180) % 360 - 180) > 20
        if i in (0, len(pts) - 1) or p[0] - lt >= 60 or g != lg or turn:
            alt = -1 if g else (int(p[3]) if isinstance(p[3], (int, float)) else None)
            out.append([int(p[0]), round(p[1], 4), round(p[2], 4), alt, None if p[4] is None else int(p[4])])
            lt, ltr, lg = p[0], tr, g
    return out


def end_airport(ap: Airports, p: list) -> dict | None:
    """Airport at a leg end if the aircraft is on the ground or low over it."""
    a = ap.near(p[1], p[2], 6.0)
    if not a:
        return None
    elev = a.pop("elev_ft")
    if p[3] == -1 or (p[3] is not None and p[3] - elev <= AGL_FT):
        return a
    return None


def crossings(cs: Countries, pts: list) -> list:
    """Countries along the track in order: [[cc, t_in, t_out], ...] (sea and gaps left out)."""
    ccs = cs.of([p[2] for p in pts], [p[1] for p in pts])
    out = []
    for p, c in zip(pts, ccs):
        if not c:
            continue
        if out and out[-1][0] == c:
            out[-1][2] = p[0]
        else:
            out.append([c, p[0], p[0]])
    return out


def callsigns(pts: list) -> list[str]:
    seen = []
    for p in pts:
        ac = p[8] if len(p) > 8 else None
        f = (ac or {}).get("flight") if isinstance(ac, dict) else None
        if f and f.strip() and f.strip() not in seen:
            seen.append(f.strip())
    return seen


def process(date: dt.date, src, out_path, ap: Airports, cs: Countries) -> dict:
    t0 = time.time()
    n_ac = n_leg = n_priv = 0
    tmp = out_path.with_suffix(".part")
    with gzip.open(tmp, "wt", encoding="utf-8") as out, tarfile.open(fileobj=src, mode="r|") as tf:
        for m in tf:
            if not m.isfile() or "trace_full_" not in m.name:
                continue
            raw = tf.extractfile(m).read()
            try:
                d = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
            except (OSError, ValueError):
                continue
            n_ac += 1
            if private(d.get("dbFlags")):
                n_priv += 1
                continue
            icao = (d.get("icao") or "").lower()
            reg_cc, _ = registry(icao)
            base = {"icao": icao, "r": d.get("r"), "t": d.get("t"), "desc": d.get("desc"), "own": d.get("ownOp"),
                    "year": d.get("year"), "db": d.get("dbFlags") or 0, "cls": aircraft_class(d.get("dbFlags"), d.get("ownOp")),
                    "reg_cc": reg_cc}
            for raw_pts in legs(d):
                pts = simplify(raw_pts)
                if len(pts) < 2:
                    continue
                n_leg += 1
                rec = dict(base, cs=callsigns(raw_pts), t0=pts[0][0], t1=pts[-1][0],
                           dep=end_airport(ap, pts[0]), arr=end_airport(ap, pts[-1]),
                           countries=crossings(cs, pts), pts=pts)
                out.write(json.dumps(rec, separators=(",", ":")) + "\n")
            if n_ac % 10000 == 0:
                print(f"  {date}: {n_ac} aircraft, {n_leg} legs, {time.time() - t0:.0f} s", file=sys.stderr, flush=True)
    tmp.replace(out_path)
    stats = {"date": str(date), "aircraft": n_ac, "legs": n_leg, "private_dropped": n_priv, "seconds": round(time.time() - t0)}
    out_path.with_suffix("").with_suffix(".meta.json").write_text(json.dumps(stats))
    return stats


def main():
    ap_ = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap_.add_argument("start")
    ap_.add_argument("end", nargs="?")
    ap_.add_argument("--tar-dir", help="read <dir>/*.tar.a? instead of downloading (one day only)")
    ap_.add_argument("--force", action="store_true", help="rebuild even if the day is cached")
    a = ap_.parse_args()
    d0 = dt.date.fromisoformat(a.start)
    d1 = dt.date.fromisoformat(a.end) if a.end else d0
    ap, cs = Airports(), Countries()
    day = d0
    while day <= d1:
        out = day_file(str(day))
        if out.exists() and not a.force:
            print(f"{day}: cached {out}")
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            if a.tar_dir:
                import glob
                parts = sorted(glob.glob(f"{a.tar_dir}/*.tar.a?"))
                p = subprocess.Popen(["cat", *parts], stdout=subprocess.PIPE, bufsize=1 << 20)
            else:
                p = stream(releases(day))
            print(f"{day}: {process(day, p.stdout, out, ap, cs)} -> {out}")
            p.wait()
        day += dt.timedelta(days=1)


if __name__ == "__main__":
    main()
