# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "shapely>=2"]
# ///
"""Questions over the flights built by day.py (run day.py for each date first).

  query.py country IL --date 2026-10-12 --class mil,state        # who flew in/over Israel, landings by airport
  query.py route --from IL --to AZ --date 2026-10-10 --end 2026-10-12   # trips, legs joined at airports
  query.py route --reg-cc AZ --via GE --date 2026-10-12           # Azerbaijani-registered aircraft over Georgia
  query.py aircraft 4X-EMF --date 2026-10-10 --end 2026-10-12     # one aircraft (registration or ICAO hex)
  query.py coverage --date 2026-10-12 [--cc IL,SY,AZ,CN]          # how well each country is seen

Every command takes --map out.html (interactive map) and --json out.json.

What the data can and cannot say:
  - only aircraft that transmit ADS-B or are multilaterated and are heard by a volunteer receiver appear. Many
    military flights switch transponders off or use addresses the database does not know; China, Iran, Syria and
    parts of Africa have very few receivers. Run `coverage` before reading an absence as "no flights".
  - "state of registry" comes from the ICAO address block, i.e. where the aircraft is registered, not who
    chartered it or who was on board. The data never shows passengers.
  - countries are land borders (Natural Earth 1:50m), not flight information regions or territorial waters.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import json
import sys
from pathlib import Path

from common import Countries, day_file

JOIN_H = 18          # legs of one aircraft are joined into an itinerary when the next take-off is within this many hours
JOIN_KM = 30         # ... and starts this close to where the last leg ended
GUESS_KM = 40        # a leg first/last seen below GUESS_FT this close to an airport gets it as a probable end
GUESS_FT = 12000


def utc(t: int) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")


_AP = None
_RT = None


def _km(a, b):
    import math
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(min(1, math.sqrt(h)))


def guess_ends(r):
    """Fill take-off / landing airports that were not heard.
    1. probable: first or last seen low (< GUESS_FT) within GUESS_KM of a large/medium airport.
    2. schedule: the callsign's timetable route (VRS standing data), using the segment the track runs along
       (it must head from the segment's origin towards its destination without a big detour). Marked, because a
       callsign says where the flight was planned to go, not where it went."""
    global _AP, _RT
    if _AP is None:
        from common import Airports, Routes
        _AP = Airports()
        try:
            _RT = Routes()
        except Exception as e:  # noqa: BLE001
            print(f"[!] route data unavailable: {e}", file=sys.stderr)
            _RT = False
    for key, p in (("dep", r["pts"][0]), ("arr", r["pts"][-1])):
        if r.get(key) or p[3] is None or p[3] > GUESS_FT:
            continue
        a = _AP.near(p[1], p[2], GUESS_KM, kinds=("large_airport", "medium_airport"))
        if a:
            a.pop("elev_ft", None)
            r[key] = dict(a, probable=True)
    if not _RT or (r.get("dep") and r.get("arr") and not r["dep"].get("probable") and not r["arr"].get("probable")):
        return
    for cs in r["cs"]:
        route = _RT.get(cs)
        if not route or len(route) < 2:
            continue
        f, l = (r["pts"][0][1], r["pts"][0][2]), (r["pts"][-1][1], r["pts"][-1][2])
        best = None
        for x, y in zip(route, route[1:]):
            X, Y = _AP.by_code(x), _AP.by_code(y)
            if not X or not Y:
                continue
            xy = _km((X["lat"], X["lon"]), (Y["lat"], Y["lon"]))
            fx, lx = _km(f, (X["lat"], X["lon"])), _km(l, (X["lat"], X["lon"]))
            fy, ly = _km(f, (Y["lat"], Y["lon"])), _km(l, (Y["lat"], Y["lon"]))
            excess = fx + _km(f, l) + ly - xy
            if lx + 1 < fx or fy + 1 < ly or excess > 0.25 * xy + 250:      # wrong way or off the line
                continue
            for k, A in (("dep", X), ("arr", Y)):
                seen = r.get(k)
                if seen and not seen.get("probable") and _km((A["lat"], A["lon"]), (seen.get("lat", 0), seen.get("lon", 0))) > 30 \
                        and seen["code"] != A["code"]:
                    excess = 1e9                                            # contradicts an airport we saw
            if best is None or excess < best[0]:
                best = (excess, X, Y)
        if best and best[0] < 1e9:
            _, X, Y = best
            for k, A in (("dep", X), ("arr", Y)):
                if not r.get(k) or r[k].get("probable"):
                    r[k] = {k2: A[k2] for k2 in ("code", "iata", "name", "cc", "type")} | {"schedule": cs}
            return


def load(d0: str, d1: str | None, need_pts: bool = False):
    a = dt.date.fromisoformat(d0)
    b = dt.date.fromisoformat(d1) if d1 else a
    missing = []
    day = a
    while day <= b:
        f = day_file(str(day))
        if not f.exists():
            missing.append(str(day))
        else:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                for line in fh:
                    r = json.loads(line)
                    r["cs"] = [c for c in r["cs"] if c.strip("@")]        # '@@@@@@@@' = no callsign transmitted
                    guess_ends(r)
                    if not need_pts:
                        r["pts"] = [r["pts"][0], r["pts"][-1]]
                    yield r
        day += dt.timedelta(days=1)
    if missing:
        print(f"[!] not built yet: {', '.join(missing)} -> run day.py {missing[0]}" + (f" {missing[-1]}" if len(missing) > 1 else ""),
              file=sys.stderr)


def cls_ok(r, classes):
    return not classes or r["cls"] in classes


def path_ccs(r) -> list[str]:
    """Countries in order: departure airport country, crossed countries, arrival airport country."""
    seq = [c for c, _, _ in r["countries"]]
    if r.get("dep") and (not seq or seq[0] != r["dep"]["cc"]):
        seq.insert(0, r["dep"]["cc"])
    if r.get("arr") and (not seq or seq[-1] != r["arr"]["cc"]):
        seq.append(r["arr"]["cc"])
    return seq


def place(a, p) -> str:
    if a:
        return f"{'~' if a.get('probable') else '*' if a.get('schedule') else ''}{a['code']} {a['name'][:28]} ({a['cc']})"
    return f"first/last seen {p[1]:.2f},{p[2]:.2f} at {'ground' if p[3] == -1 else (str(p[3]) + ' ft' if p[3] is not None else '?')}"


def ac_label(r) -> str:
    cs = "/".join(r["cs"][:2]) or "-"
    return f"{r['r'] or r['icao']} {r['t'] or ''} {cs}".strip()


def row(r) -> dict:
    return {"icao": r["icao"], "reg": r["r"], "type": r["t"], "desc": r["desc"], "operator": r["own"], "class": r["cls"],
            "registry": r["reg_cc"], "callsigns": r["cs"], "start": utc(r["t0"]), "end": utc(r["t1"]),
            "from": r["dep"], "to": r["arr"], "first_seen": r["pts"][0][:4], "last_seen": r["pts"][-1][:4],
            "countries": path_ccs(r)}


# ------------------------------------------------------------------ itineraries
def itineraries(rows: list[dict]) -> list[list[dict]]:
    """Join consecutive legs of the same aircraft that end and start at the same place (a stop)."""
    import math
    by = collections.defaultdict(list)
    for r in rows:
        by[r["icao"]].append(r)
    out = []
    for legs in by.values():
        legs.sort(key=lambda r: r["t0"])
        cur = [legs[0]]
        for r in legs[1:]:
            a, b = cur[-1]["pts"][-1], r["pts"][0]
            km = 6371 * math.hypot(math.radians(b[1] - a[1]), math.radians(b[2] - a[2]) * math.cos(math.radians(a[1])))
            if r["t0"] - cur[-1]["t1"] <= JOIN_H * 3600 and km <= JOIN_KM:
                cur.append(r)
            else:
                out.append(cur)
                cur = [r]
        out.append(cur)
    return out


def itin_ccs(it) -> list[str]:
    seq = []
    for r in it:
        for c in path_ccs(r):
            if not seq or seq[-1] != c:
                seq.append(c)
    return seq


def stops(it) -> list[dict]:
    return [r["arr"] for r in it[:-1] if r.get("arr")]


# ------------------------------------------------------------------ commands
def cmd_country(a):
    cc = a.cc.upper()
    classes = set(a.cls.split(",")) if a.cls else set()
    hits = []
    for r in load(a.date, a.end, need_pts=bool(a.map)):
        if not cls_ok(r, classes) or (a.reg_cc and r["reg_cc"] != a.reg_cc.upper()):
            continue
        touch = cc in path_ccs(r)
        if touch:
            hits.append(r)
    landed = [r for r in hits if r.get("arr") and r["arr"]["cc"] == cc]
    departed = [r for r in hits if r.get("dep") and r["dep"]["cc"] == cc]
    over = [r for r in hits if r not in landed and r not in departed]
    print(f"# {cc}  {a.date}{' .. ' + a.end if a.end else ''}  class={a.cls or 'all'}{'  registry=' + a.reg_cc.upper() if a.reg_cc else ''}")
    print(f"legs touching {cc}: {len(hits)}  (aircraft: {len({r['icao'] for r in hits})})")
    print(f"  landed in {cc}: {len(landed)}   took off in {cc}: {len(departed)}   overflew / ends unknown: {len(over)}")
    print("by class:", dict(collections.Counter(r["cls"] for r in hits)))
    print("by state of registry:", dict(collections.Counter(r["reg_cc"] or "?" for r in hits).most_common(25)))
    print(f"\nlandings in {cc} by airport:")
    for (c_, name), n in collections.Counter((("~" if r["arr"].get("probable") else "") + r["arr"]["code"], r["arr"]["name"])
                                              for r in landed).most_common(30):
        print(f"  {n:4d}  {c_}  {name}")
    def code(x):
        return ("~" if x.get("probable") else "*" if x.get("schedule") else "") + x["code"] if x else "?"
    pairs = collections.Counter(f"{code(r['dep'])} -> {code(r['arr'])}" for r in hits)
    print("\ntop routes (? = not seen; ~ = probable, last seen low nearby; * = from the callsign's timetable):")
    for k, n in pairs.most_common(15):
        print(f"  {n:4d}  {k}")
    lim = a.limit
    print(f"\nlegs ({min(lim, len(hits))} of {len(hits)}, by time):")
    for r in sorted(hits, key=lambda r: r["t0"])[:lim]:
        print(f"  {utc(r['t0'])}  [{r['cls']:5s}] {r['reg_cc'] or '?':2s}  {ac_label(r):34s} {(r['own'] or '')[:26]:26s} "
              f"{place(r['dep'], r['pts'][0])}  ->  {place(r['arr'], r['pts'][-1])}   via {'-'.join(path_ccs(r))}")
    finish(a, [row(r) for r in hits], hits)


def match_trip(legs, frm, via, to, loose=False) -> bool:
    """from/to are the countries of the first take-off and last landing airport (seen, probable or timetable);
    a track that is merely lost over a country did not land there. --loose falls back to first/last country crossed."""
    seq = itin_ccs(legs)
    s = (legs[0].get("dep") or {}).get("cc") or (seq[0] if loose and seq else None)
    e = (legs[-1].get("arr") or {}).get("cc") or (seq[-1] if loose and seq else None)
    if frm and s not in frm:
        return False
    if to and e not in to:
        return False
    return all(v in seq for v in via)


def match_seq(seq, frm, via, to) -> bool:
    if frm and (not seq or seq[0] not in frm):
        return False
    if to and (not seq or seq[-1] not in to):
        return False
    if via:
        inner = seq[1:-1] if (frm or to) else seq
        if not all(v in inner or v in seq for v in via):
            return False
    return True


def cmd_route(a):
    frm = set(a.frm.upper().split(",")) if a.frm else set()
    to = set(a.to.upper().split(",")) if a.to else set()
    via = a.via.upper().split(",") if a.via else []
    classes = set(a.cls.split(",")) if a.cls else set()
    rows = [r for r in load(a.date, a.end, need_pts=bool(a.map))
            if cls_ok(r, classes) and (not a.reg_cc or r["reg_cc"] == a.reg_cc.upper())]
    found = []
    for it in itineraries(rows):
        # any stretch of consecutive legs can be the trip asked about (a round trip A -> B -> A contains A -> B);
        # take the shortest matching stretches, left to right, without overlap
        i = 0
        while i < len(it):
            hit = next((j for j in range(i, min(len(it), i + a.max_stops + 1)) if match_trip(it[i:j + 1], frm, via, to, a.loose)), None)
            if hit is None:
                i += 1
                continue
            found.append(it[i:hit + 1])
            i = hit + 1
    print(f"# itineraries from={a.frm or '*'} via={a.via or '*'} to={a.to or '*'}  registry={a.reg_cc or '*'}  "
          f"class={a.cls or 'all'}  {a.date}{' .. ' + a.end if a.end else ''}: {len(found)}")
    out = []
    for it in sorted(found, key=lambda it: it[0]["t0"])[:a.limit]:
        r0 = it[0]
        print(f"\n{ac_label(r0)}  [{r0['cls']}] registry {r0['reg_cc'] or '?'}  {r0['own'] or ''}")
        for r in it:
            print(f"   {utc(r['t0'])} {place(r['dep'], r['pts'][0])}\n   {utc(r['t1'])}  -> {place(r['arr'], r['pts'][-1])}"
                  f"   (via {'-'.join(path_ccs(r))})")
        print(f"   countries: {' > '.join(itin_ccs(it))}")
        out.append({"aircraft": {k: r0[k] for k in ("icao", "r", "t", "desc", "own", "cls", "reg_cc")},
                    "countries": itin_ccs(it), "stops": stops(it), "legs": [row(r) for r in it]})
    finish(a, out, [r for it in found for r in it])


def cmd_aircraft(a):
    key = a.id.strip().lower()
    legs = [r for r in load(a.date, a.end, need_pts=bool(a.map)) if r["icao"] == key or (r["r"] or "").lower() == key]
    if not legs:
        print(f"{a.id}: not seen in these days (or flagged private: PIA/LADD aircraft are not stored)")
        return
    r0 = legs[0]
    print(f"# {r0['r'] or '-'}  icao {r0['icao']}  {r0['t'] or ''} {r0['desc'] or ''}  operator {r0['own'] or '-'}  "
          f"class {r0['cls']}  registry {r0['reg_cc'] or '?'}")
    for r in sorted(legs, key=lambda r: r["t0"]):
        print(f"  {utc(r['t0'])} {place(r['dep'], r['pts'][0])}\n  {utc(r['t1'])}  -> {place(r['arr'], r['pts'][-1])}"
              f"   callsign {'/'.join(r['cs']) or '-'}   via {'-'.join(path_ccs(r))}")
    finish(a, [row(r) for r in legs], legs)


def cmd_coverage(a):
    """Per country: aircraft seen, and of legs that end low inside it, the share whose landing airport was seen.
    A low share means receivers there miss the last part of flights (and miss low or quiet traffic altogether)."""
    want = set(a.cc.upper().split(",")) if a.cc else None
    seen = collections.defaultdict(set)
    ends = collections.Counter()
    ends_ok = collections.Counter()
    cs = Countries()
    rows = list(load(a.date, a.end))
    last = cs.of([r["pts"][-1][2] for r in rows], [r["pts"][-1][1] for r in rows])
    for r, lc in zip(rows, last):
        for c in path_ccs(r):
            seen[c].add(r["icao"])
        if r.get("arr") and not r["arr"].get("probable") and not r["arr"].get("schedule"):
            ends[r["arr"]["cc"]] += 1
            ends_ok[r["arr"]["cc"]] += 1      # landing heard at the airport
        elif lc and r["pts"][-1][3] is not None and r["pts"][-1][3] < 10000:
            ends[lc] += 1          # descending inside the country, then lost before the airport
    ccs = sorted(want or seen, key=lambda c: -len(seen.get(c, ())))
    print(f"# coverage {a.date}{' .. ' + a.end if a.end else ''}")
    print(f"{'cc':3s} {'country':24s} {'aircraft':>8s} {'low ends':>8s} {'landing seen':>12s}")
    out = []
    for c in ccs[: a.limit]:
        n, e = len(seen.get(c, ())), ends[c]
        share = ends_ok[c] / e if e else None
        print(f"{c:3s} {cs.name.get(c, '?')[:24]:24s} {n:8d} {e:8d} {('%.0f %%' % (100 * share)) if share is not None else '-':>12s}")
        out.append({"cc": c, "name": cs.name.get(c), "aircraft": n, "low_ends": e, "landing_seen": share})
    finish(a, out, [])


# ------------------------------------------------------------------ output
COLORS = {"mil": "#d62728", "state": "#9467bd", "civil": "#1f77b4"}


def finish(a, data, legs):
    if a.json:
        Path(a.json).write_text(json.dumps(data, ensure_ascii=False, indent=1))
        print(f"\n-> {a.json}")
    if a.map and legs:
        import mapview
        title = "flow-sleuth: " + " ".join(x for x in sys.argv[1:] if not x.startswith("--map") and not x.endswith(".html"))
        mapview.write(a.map, legs, title, path_ccs, Countries().name)
        print(f"-> {a.map} ({len(legs)} legs)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(s):
        s.add_argument("--date", required=True, help="YYYY-MM-DD (UTC day)")
        s.add_argument("--end", help="last day of a range")
        s.add_argument("--class", dest="cls", help="mil,state,civil (comma list); default all")
        s.add_argument("--reg-cc", help="state of registry from the ICAO address, e.g. AZ")
        s.add_argument("--limit", type=int, default=60)
        s.add_argument("--map")
        s.add_argument("--json")

    s = sub.add_parser("country"); s.add_argument("cc"); common(s); s.set_defaults(f=cmd_country)
    s = sub.add_parser("route"); s.add_argument("--from", dest="frm"); s.add_argument("--via"); s.add_argument("--to")
    s.add_argument("--loose", action="store_true", help="when an airport was not seen, use the first/last country crossed")
    s.add_argument("--max-stops", type=int, default=1, help="intermediate landings allowed in one trip (default 1)")
    common(s); s.set_defaults(f=cmd_route)
    s = sub.add_parser("aircraft"); s.add_argument("id", help="registration (4X-EMF) or ICAO hex"); common(s); s.set_defaults(f=cmd_aircraft)
    s = sub.add_parser("coverage"); s.add_argument("--cc", help="comma list; default all"); common(s); s.set_defaults(f=cmd_coverage)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
