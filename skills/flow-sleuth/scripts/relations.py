# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "shapely>=2"]
# ///
"""Links between two or more countries: trade, shipping, flights. One report page per run.

  relations.py IL AZ --out out/il-az
  relations.py IL AZ SY --flights 2026-10-10:2026-10-12 --out out/il-az-sy

For every pair of the countries given:
  trade     UN Comtrade (public preview API, no key): yearly totals both ways as each side reports them (a gap
            or a big mismatch between the two sides is itself a signal: embargoes, re-routing via third
            countries, suppressed partner data), the last 24 months, and the top product chapters (HS2).
  shipping  IMF PortWatch (no key): each country's monthly port calls and seaborne import/export from AIS, the
            busiest ports over the last 30 days, and the modelled baseline trade between each port of one country
            and the other country (an IMF estimate, not a measurement).
  flights   from the days already built with day.py: flights between the two countries (take-off in one,
            landing in the other, either way), military and state aircraft of one seen in the other, and
            aircraft registered in one flying over the other. Coverage limits as in query.py.

The page lists measurable signals, not a verdict on whether relations are "good" or "bad": trade up or down,
reporting gaps, direct flights or none, state aircraft visits. Interpretation is left to the reader.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import itertools
import json
import sys
import time
import urllib.parse
from pathlib import Path

from common import CACHE, Countries, day_file, get

CT = "https://comtradeapi.un.org/public/v1/preview"
CT_REF = "https://comtradeapi.un.org/files/v1/app/reference"
PW = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"


# ------------------------------------------------------------------ cached HTTP
def cached_json(key: str, url: str, max_age_days: float = 30, pause: float = 0.0):
    f = CACHE / "http" / (key.replace("/", "_") + ".json")
    if f.exists() and time.time() - f.stat().st_mtime < max_age_days * 86400:
        return json.loads(f.read_text())
    for attempt in range(4):
        try:
            if pause:
                time.sleep(pause)
            d = json.loads(get(url, timeout=90))
            break
        except Exception as e:  # noqa: BLE001  (rate limits and timeouts: back off, then give up)
            if attempt == 3:
                print(f"[!] {url[:120]}: {e}", file=sys.stderr)
                return None
            time.sleep(3 * (attempt + 1))
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(d))
    return d


def refs():
    rep = cached_json("ct_reporters", f"{CT_REF}/Reporters.json", 180)["results"]
    hs = cached_json("ct_hs", f"{CT_REF}/HS.json", 180)["results"]
    by2 = {r["reporterCodeIsoAlpha2"]: r for r in rep if r.get("reporterCodeIsoAlpha2") and not r.get("isGroup")
           and (r.get("entryExpiredDate") in (None, ""))}
    hs2 = {h["id"]: h["text"].split(" - ", 1)[-1] for h in hs if len(h["id"]) == 2 and h["id"].isdigit()}
    return by2, hs2


# ------------------------------------------------------------------ trade
def ct(reps: list[int], pars: list[int], period: str, freq: str = "A", cmd: str = "TOTAL", flows: str = "X,M"):
    """One preview call for several reporters and partners (the preview API allows lists, one period, 500 rows)."""
    rs, ps = ",".join(map(str, sorted(reps))), ",".join(map(str, sorted(pars)))
    key = f"ct_{freq}_{rs}_{ps}_{period}_{cmd}_{flows}"
    url = f"{CT}/C/{freq}/HS?" + urllib.parse.urlencode({"reporterCode": rs, "partnerCode": ps, "period": period,
                                                          "flowCode": flows, "cmdCode": cmd})
    recent = period >= str(dt.date.today().year - 1)
    d = cached_json(key, url, 7 if recent else 90, pause=1.5)
    return [r for r in (d or {}).get("data") or []
            if r.get("partner2Code") in (0, None) and r.get("motCode") in (0, None) and r.get("customsCode") in ("C00", None)
            and r["reporterCode"] != r["partnerCode"]]


def trade_all(codes: dict, years: list[int], months: list[str], hs2: dict) -> dict:
    """codes: cc -> Comtrade numeric code. Returns per ordered pair 'A-B' the flows as reported by A."""
    cc_of = {v: k for k, v in codes.items()}
    allc = list(codes.values())
    yearly, monthly = {}, {}
    for y in years:
        for r in ct(allc, allc, str(y)):
            yearly.setdefault(y, {})[(cc_of[r["reporterCode"]], cc_of[r["partnerCode"]], r["flowCode"])] = r["primaryValue"]
    for p in months:
        for r in ct(allc, allc, p, "M"):
            monthly.setdefault(p, {})[(cc_of[r["reporterCode"]], cc_of[r["partnerCode"]], r["flowCode"])] = r["primaryValue"]
    top = {}
    for c, code in codes.items():
        for o, ocode in codes.items():
            if o == c:
                continue
            for y in reversed(years):            # latest year this reporter reported this partner
                if any(k[0] == c and k[1] == o for k in yearly.get(y, {})):
                    for flow in ("X", "M"):
                        for r in ct([code], [ocode], str(y), "A", "AG2", flow):
                            top.setdefault((c, o, flow), {"year": y, "items": []})["items"].append(
                                {"hs": r["cmdCode"], "name": hs2.get(r["cmdCode"], r["cmdCode"]), "usd": r["primaryValue"]})
                    break
    for v in top.values():
        v["items"] = sorted(v["items"], key=lambda i: -(i["usd"] or 0))[:8]
    return {"yearly": yearly, "monthly": monthly, "top": top}


def trade_pair(A: str, B: str, T: dict, years: list[int], months: list[str]) -> dict:
    def row(d):
        out = {}
        for tag, rep, par in (("a", A, B), ("b", B, A)):
            for f in ("X", "M"):
                if (rep, par, f) in d:
                    out[f"{tag}_{f}"] = d[(rep, par, f)]
        return out
    top = {}
    for tag, rep, par in (("a", A, B), ("b", B, A)):
        for f in ("X", "M"):
            if (rep, par, f) in T["top"]:
                top[f"{tag}_{f}"] = T["top"][(rep, par, f)]
    return {"yearly": [dict(year=y, **row(T["yearly"].get(y, {}))) for y in years],
            "monthly": [dict(month=f"{p[:4]}-{p[4:]}", **row(T["monthly"].get(p, {}))) for p in months], "top": top}


MIN_USD = 5e6          # below this a missing or mismatching figure is noise, not a signal


def trade_signals(A: str, B: str, t: dict) -> list[str]:
    s = []
    ys = [r for r in t["yearly"] if any(k in r for k in ("a_X", "a_M", "b_X", "b_M"))]
    if not ys:
        return [f"no trade reported between {A} and {B} by either side in these years"]

    def tot(r, side):
        return (r.get(f"{side}_X") or 0) + (r.get(f"{side}_M") or 0)
    for r in ys:
        ha, hb = ("a_X" in r or "a_M" in r), ("b_X" in r or "b_M" in r)
        if hb and not ha and tot(r, "b") >= MIN_USD:
            s.append(f"{r['year']}: {A} reported no trade with {B}, while {B} reported ${tot(r, 'b') / 1e6:,.0f} M "
                     f"(partner data withheld, an embargo, or not yet published)")
        elif ha and not hb and tot(r, "a") >= MIN_USD:
            s.append(f"{r['year']}: {B} reported no trade with {A}, while {A} reported ${tot(r, 'a') / 1e6:,.0f} M")
    # each direction: the exporter's figure against the importer's (mirror statistics), years grouped
    for ex, im, X, M in (("a_X", "b_M", A, B), ("b_X", "a_M", B, A)):
        bad = [(r["year"], r[ex], r[im]) for r in ys if r.get(ex) and r.get(im) and max(r[ex], r[im]) >= MIN_USD
               and abs(r[im] / r[ex] - 1) > 0.35]
        if bad:
            yrs = f"{bad[0][0]}" if len(bad) == 1 else f"{bad[0][0]}–{bad[-1][0]}" if len(bad) == bad[-1][0] - bad[0][0] + 1 \
                else ", ".join(str(b[0]) for b in bad)

            def rng(v):
                lo, hi = min(v), max(v)

                def f(x):
                    return f"${x / 1e9:.1f} bn" if x >= 1e9 else f"${x / 1e6:,.0f} M"
                return f(lo) if lo == hi or len(v) == 1 else f"{f(lo)}–{f(hi)}"
            gaps = [m / x - 1 for _, x, m in bad]
            s.append(f"{yrs}: {X} reports {rng([b[1] for b in bad])} of exports to {M} a year, {M} reports "
                     f"{rng([b[2] for b in bad])} of imports from {X} ({min(gaps):+.0%} to {max(gaps):+.0%}). "
                     f"Freight and insurance explain 5–10 %; more points to goods recorded under another origin or "
                     f"destination, transit via third countries, or confidential trade.")
    best = [(r["year"], max(tot(r, "a"), tot(r, "b"))) for r in ys]
    if len(best) >= 2:
        (y0, v0), (y1, v1) = best[-2], best[-1]
        if v0 >= 20e6:
            s.append(f"trade {y0} -> {y1}: {v1 / v0 - 1:+.0%} (${v0 / 1e9:.2f} bn -> ${v1 / 1e9:.2f} bn, larger of the two sides' figures)")
        elif v1 >= 20e6:
            s.append(f"trade {y1}: ${v1 / 1e6:,.0f} M, up from under $20 M in {y0}")
    return s


# ------------------------------------------------------------------ shipping
def pw_query(layer: str, where: str, **kw):
    q = {"where": where, "f": "json", **kw}
    url = f"{PW}/{layer}/FeatureServer/0/query?" + urllib.parse.urlencode(q)
    d = cached_json("pw_" + layer + "_" + str(abs(hash(url))), url, 3)
    return [f["attributes"] for f in (d or {}).get("features", [])]


def shipping(iso3: str, other_iso3: list[str]) -> dict:
    monthly = pw_query("Monthly_TradeNow", f"ISO3='{iso3}'", outFields="date,ais_portcalls_container,ais_portcalls_tanker,"
                       "ais_portcalls_dry_bulk,ais_portcalls_general_cargo,ais_portcalls_roro,volume_import_total,"
                       "volume_export_total,value_import_total,value_export_total", orderByFields="date")
    since = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    stats = json.dumps([{"statisticType": "sum", "onStatisticField": f, "outStatisticFieldName": "s_" + f}
                        for f in ("portcalls", "portcalls_tanker", "import", "export")])   # alias must differ from the field
    ports = pw_query("Daily_Ports_Data", f"ISO3='{iso3}' AND date >= DATE '{since}'", outStatistics=stats,
                     groupByFieldsForStatistics="portname", orderByFields="s_portcalls DESC")[:12]
    ports = [{k[2:] if k.startswith("s_") else k: v for k, v in r.items()} for r in ports]
    links = {}
    for o in other_iso3:
        rows = pw_query("spillovers_trade", f"from_iso3='{iso3}' AND to_iso3='{o}'",
                        outFields="from_portname,from_lat,from_lon,to_lat,to_lon,industry,daily_export_value_at_risk,daily_import_value_at_risk",
                        resultRecordCount=2000)
        agg = {}
        for r in rows:
            k = r["from_portname"]
            x = agg.setdefault(k, {"port": k, "lat": r["from_lat"], "lon": r["from_lon"], "to_lat": r["to_lat"], "to_lon": r["to_lon"],
                                   "export_usd_day": 0.0, "import_usd_day": 0.0, "industries": {}})
            e, i = r.get("daily_export_value_at_risk") or 0, r.get("daily_import_value_at_risk") or 0
            x["export_usd_day"] += e
            x["import_usd_day"] += i
            if r["industry"] and r["industry"] != "Total":
                x["industries"][r["industry"]] = x["industries"].get(r["industry"], 0) + e + i
        for x in agg.values():
            x["industries"] = sorted(x["industries"], key=x["industries"].get, reverse=True)[:3]
        links[o] = sorted(agg.values(), key=lambda x: -(x["export_usd_day"] + x["import_usd_day"]))[:10]
    return {"monthly": monthly[-36:], "ports_30d": ports, "port_links": links}


# ------------------------------------------------------------------ flights
def flights(ccs: list[str], span: str | None):
    if not span:
        return None, []
    d0, _, d1 = span.partition(":")
    from query import load, path_ccs
    pairs = {f"{a}-{b}": {"direct": 0, "inferred": 0, "direct_mil_state": 0, "examples": []} for a, b in itertools.permutations(ccs, 2)}
    visits = {f"{a}-in-{b}": [] for a, b in itertools.permutations(ccs, 2)}
    over = {f"{a}-over-{b}": 0 for a, b in itertools.permutations(ccs, 2)}
    legs = []
    for r in load(d0, d1 or None, need_pts=True):
        seq = path_ccs(r)
        if not seq:
            continue
        # a flight between two countries needs both ends: heard at the airport, or probable (lost low nearby), or
        # from the callsign's timetable. A track first heard over a country did not necessarily start there.
        s, e = (r["dep"] or {}).get("cc"), (r["arr"] or {}).get("cc")
        keep = False
        inferred = any((r[k] or {}).get("probable") or (r[k] or {}).get("schedule") for k in ("dep", "arr"))
        kind = "inferred" if inferred else "direct"
        if s in ccs and e in ccs and s != e:
            p = pairs[f"{s}-{e}"]
            p[kind] += 1
            if r["cls"] != "civil":
                p["direct_mil_state"] += 1
            if len(p["examples"]) < 40:
                p["examples"].append({"reg": r["r"], "type": r["t"], "cs": r["cs"], "cls": r["cls"], "registry": r["reg_cc"],
                                      "op": r["own"], "from": r["dep"], "to": r["arr"], "kind": kind})
            keep = True
        rc = r["reg_cc"]
        if rc in ccs:
            for c in ccs:
                if c != rc and c in seq:
                    over[f"{rc}-over-{c}"] += 1
                    if r["cls"] != "civil":
                        visits[f"{rc}-in-{c}"].append({"reg": r["r"], "type": r["t"], "desc": r["desc"], "cs": r["cs"],
                                                       "cls": r["cls"], "op": r["own"], "from": r["dep"], "to": r["arr"],
                                                       "t0": r["t0"], "countries": seq})
                        keep = True
        if keep:
            legs.append(r)
    return {"span": span, "pairs": pairs, "state_visits": visits, "overflights": over}, legs


# ------------------------------------------------------------------ page
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cc", nargs="+", help="ISO 3166 alpha-2 codes, 2 or more")
    ap.add_argument("--years", default=f"{dt.date.today().year - 6}-{dt.date.today().year - 1}")
    ap.add_argument("--months", type=int, default=24, help="monthly trade for the last N months (0 = skip)")
    ap.add_argument("--flights", help="YYYY-MM-DD:YYYY-MM-DD, days already built with day.py")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    ccs = [c.upper() for c in a.cc]
    if len(ccs) < 2:
        raise SystemExit("give at least two countries")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    by2, hs2 = refs()
    names = Countries().name
    missing = [c for c in ccs if c not in by2]
    if missing:
        raise SystemExit(f"not a Comtrade reporter: {missing}")
    y0, y1 = map(int, a.years.split("-"))
    years = list(range(y0, y1 + 1))
    today = dt.date.today().replace(day=1)
    months = []
    m = today
    for _ in range(a.months):
        m = (m - dt.timedelta(days=1)).replace(day=1)
        months.append(m.strftime("%Y%m"))
    months.reverse()

    rep = {"countries": {c: {"name": by2[c]["text"], "iso3": by2[c]["reporterCodeIsoAlpha3"]} for c in ccs},
           "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"), "pairs": {}, "shipping": {}}
    print(f"trade {' '.join(ccs)}: {len(years) + len(months)} periods ...", file=sys.stderr, flush=True)
    T = trade_all({c: by2[c]["reporterCode"] for c in ccs}, years, months, hs2)
    for A, B in itertools.combinations(ccs, 2):
        t = trade_pair(A, B, T, years, months)
        rep["pairs"][f"{A}-{B}"] = {"a": A, "b": B, "trade": t, "signals": trade_signals(A, B, t)}
    for c in ccs:
        print(f"shipping {c} ...", file=sys.stderr, flush=True)
        rep["shipping"][c] = shipping(by2[c]["reporterCodeIsoAlpha3"], [by2[o]["reporterCodeIsoAlpha3"] for o in ccs if o != c])
    fl, legs = flights(ccs, a.flights)
    rep["flights"] = fl
    if legs:
        import mapview
        from query import path_ccs
        mapview.write(str(out / "flights.html"), legs, f"Flights {' · '.join(ccs)} {a.flights}", path_ccs, names)
    (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1))
    import report_page
    report_page.write(out / "index.html", rep, bool(legs))
    print(f"-> {out / 'index.html'}")
    for k, p in rep["pairs"].items():
        print(f"\n{k}:")
        for s in p["signals"]:
            print("  -", s)


if __name__ == "__main__":
    main()
