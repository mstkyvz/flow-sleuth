# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "shapely>=2"]
# ///
"""Offline tests: no network. Airports and timetable routes are replaced by small stubs.
Run: uv run skills/flow-sleuth/tests/test_core.py"""
from __future__ import annotations

import gzip
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
TMP = tempfile.mkdtemp(prefix="flow-sleuth-test-")
os.environ["FLOW_SLEUTH_CACHE"] = TMP          # never touch the real cache

import common  # noqa: E402
import day  # noqa: E402
import mapview  # noqa: E402
import query  # noqa: E402
import relations  # noqa: E402
import report_page  # noqa: E402

AIRPORTS = {
    "LLBG": {"code": "LLBG", "iata": "TLV", "name": "Ben Gurion International Airport", "cc": "IL", "type": "large_airport",
             "lat": 32.0114, "lon": 34.8867, "elev_ft": 135},
    "UBBB": {"code": "UBBB", "iata": "GYD", "name": "Heydar Aliyev International Airport", "cc": "AZ", "type": "large_airport",
             "lat": 40.4675, "lon": 50.0467, "elev_ft": 10},
    "LHBP": {"code": "LHBP", "iata": "BUD", "name": "Budapest Liszt Ferenc International Airport", "cc": "HU", "type": "large_airport",
             "lat": 47.4298, "lon": 19.2611, "elev_ft": 495},
    "LCLK": {"code": "LCLK", "iata": "LCA", "name": "Larnaca International Airport", "cc": "CY", "type": "large_airport",
             "lat": 34.8751, "lon": 33.6249, "elev_ft": 8},
}


class FakeAirports:
    def near(self, lat, lon, km=6.0, kinds=None):
        best = None
        for a in AIRPORTS.values():
            d = query._km((lat, lon), (a["lat"], a["lon"]))
            if d <= km and (best is None or d < best[0]):
                best = (d, a)
        if not best:
            return None
        a = best[1]
        return {"code": a["code"], "iata": a["iata"], "name": a["name"], "cc": a["cc"], "type": a["type"],
                "km": round(best[0], 1), "elev_ft": a["elev_ft"]}

    def by_code(self, code):
        a = AIRPORTS.get(code)
        return dict(a) if a else None


class FakeRoutes:
    R = {"AIZ321": ["LLBG", "UBBB"], "WZZ5618": ["LLBG", "LHBP", "UBBB"], "XYZ1": ["LCLK", "LHBP"]}

    def get(self, cs):
        return self.R.get((cs or "").strip().upper())


def leg(icao, t0, t1, pts, dep=None, arr=None, cls="civil", reg_cc="IL", cs=("TEST1",), countries=None):
    return {"icao": icao, "r": icao.upper(), "t": "A320", "desc": "AIRBUS A-320", "own": None, "year": None, "db": 0,
            "cls": cls, "reg_cc": reg_cc, "cs": list(cs), "t0": t0, "t1": t1, "dep": dep, "arr": arr,
            "countries": countries or [], "pts": pts}


def ap(code, **kw):
    a = {k: AIRPORTS[code][k] for k in ("code", "iata", "name", "cc", "type")}
    a.update(kw)
    return a


class TestRegistry(unittest.TestCase):
    def test_blocks(self):
        self.assertEqual(common.registry("738488")[0], "IL")
        self.assertEqual(common.registry("600900")[0], "AZ")
        self.assertEqual(common.registry("ae74de")[0], "US")
        self.assertEqual(common.registry("~2a0001"), ("", ""))       # non-ICAO (TIS-B) address
        self.assertEqual(common.registry(None), ("", ""))

    def test_class_and_privacy(self):
        self.assertEqual(common.aircraft_class(1, None), "mil")
        self.assertEqual(common.aircraft_class(0, "Qatar Amiri Flight"), "state")
        self.assertEqual(common.aircraft_class(0, "Government of the Netherlands"), "state")
        self.assertEqual(common.aircraft_class(0, "Ryanair"), "civil")
        self.assertEqual(common.aircraft_class(1, "Royal Air Force"), "mil")
        self.assertTrue(common.private(4))
        self.assertTrue(common.private(8 | 1))
        self.assertFalse(common.private(1 | 2))


class TestCountries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cs = common.Countries()

    def test_points(self):
        # Tel Aviv, Baku, eastern Mediterranean (sea), Damascus
        got = self.cs.of([34.78, 49.87, 33.0, 36.29], [32.08, 40.41, 33.5, 33.51])
        self.assertEqual(got, ["IL", "AZ", "", "SY"])

    def test_unknown_code(self):
        with self.assertRaises(SystemExit):
            self.cs.polygon("XX")


class TestDay(unittest.TestCase):
    def trace(self):
        # readsb trace: [dt, lat, lon, alt, gs, track, flags, vrate, aircraft]
        return {"timestamp": 1_000_000, "trace": [
            [0, 32.00, 34.88, "ground", 10, 90, 0, None, {"flight": "AIZ321 "}],
            [30, 32.01, 34.89, 1500, 160, 90, 0, None, None],
            [90, 32.10, 35.00, 9000, 300, 45, 0, None, None],
            [95, 32.11, 35.01, 9100, 300, 46, 0, None, None],          # < 60 s later, no turn: dropped by simplify
            [200, 32.50, 35.50, 30000, 450, 45, 0, None, {"flight": "@@@@@@@@"}],
            [9000, 40.46, 50.04, "ground", 20, 10, 2, None, {"flight": "AIZ322"}],   # new leg flag
            [9100, 40.50, 50.10, 2000, 170, 10, 0, None, None],
            [9100 + 3 * 3600, 41.0, 50.5, 30000, 450, 10, 0, None, None],      # gap > 2 h: another leg
        ]}

    def test_legs_split(self):
        legs = list(day.legs(self.trace()))
        self.assertEqual([len(x) for x in legs], [5, 2, 1])
        self.assertEqual(legs[1][0][0], 1_009_000)                   # absolute time

    def test_simplify_keeps_ends_and_ground(self):
        pts = day.simplify(list(day.legs(self.trace()))[0])
        self.assertEqual(pts[0][3], -1)                              # ground marked -1
        self.assertEqual(pts[-1][0], 1_000_200)
        self.assertNotIn(1_000_095, [p[0] for p in pts])

    def test_callsigns(self):
        legs = list(day.legs(self.trace()))
        self.assertEqual(day.callsigns(legs[0]), ["AIZ321", "@@@@@@@@"])

    def test_end_airport(self):
        fa = FakeAirports()
        self.assertEqual(day.end_airport(fa, [0, 32.0, 34.88, -1, 0])["code"], "LLBG")
        self.assertEqual(day.end_airport(fa, [0, 32.02, 34.9, 2000, 0])["code"], "LLBG")   # low over it
        self.assertIsNone(day.end_airport(fa, [0, 32.02, 34.9, 9000, 0]))                 # too high
        self.assertIsNone(day.end_airport(fa, [0, 33.0, 34.0, -1, 0]))                    # no airport near

    def test_crossings(self):
        cs = common.Countries()
        pts = [[0, 32.08, 34.78, 0, 0], [60, 33.5, 33.0, 0, 0], [120, 35.1, 33.4, 0, 0], [180, 35.0, 33.5, 0, 0]]
        got = day.crossings(cs, pts)
        self.assertEqual(got[0], ["IL", 0, 0])
        self.assertEqual(got[-1][0], "CY")
        self.assertEqual(got[-1][1:], [120, 180])

    def test_process_drops_private(self):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for icao, flags in (("738488", 0), ("a00001", 8)):
                d = dict(self.trace(), icao=icao, r="X", t="A320", dbFlags=flags)
                raw = gzip.compress(json.dumps(d).encode())
                ti = tarfile.TarInfo(f"traces/{icao[-2:]}/trace_full_{icao}.json")
                ti.size = len(raw)
                tf.addfile(ti, io.BytesIO(raw))
        buf.seek(0)
        out = Path(TMP) / "days" / "2000-01-01.jsonl.gz"
        out.parent.mkdir(parents=True, exist_ok=True)
        st = day.process(__import__("datetime").date(2000, 1, 1), buf, out, FakeAirports(), common.Countries())
        self.assertEqual(st["aircraft"], 2)
        self.assertEqual(st["private_dropped"], 1)
        rows = [json.loads(x) for x in gzip.open(out, "rt")]
        self.assertTrue(rows and all(r["icao"] == "738488" for r in rows))
        self.assertEqual(rows[0]["dep"]["code"], "LLBG")
        self.assertEqual(rows[0]["reg_cc"], "IL")


class TestQuery(unittest.TestCase):
    def setUp(self):
        query._AP, query._RT = FakeAirports(), FakeRoutes()

    def test_guess_probable_and_schedule(self):
        # take-off heard at LLBG; track lost at cruise over Azerbaijan -> landing from the timetable
        r = leg("738488", 0, 9000, [[0, 32.0, 34.88, -1, 0], [9000, 40.9, 47.5, 31000, 450]], dep=ap("LLBG"), cs=("AIZ321",))
        query.guess_ends(r)
        self.assertEqual(r["arr"]["code"], "UBBB")
        self.assertEqual(r["arr"]["schedule"], "AIZ321")
        # lost low near an airport, no timetable -> probable
        r = leg("738489", 0, 900, [[0, 34.5, 33.0, 30000, 400], [900, 34.95, 33.7, 5000, 250]], cs=("NOPE1",))
        query.guess_ends(r)
        self.assertEqual(r["arr"]["code"], "LCLK")
        self.assertTrue(r["arr"]["probable"])

    def test_schedule_rejects_wrong_direction(self):
        # flying away from the timetable's destination: no schedule airport
        r = leg("738490", 0, 900, [[0, 40.0, 48.0, 30000, 400], [900, 38.0, 44.0, 30000, 400]], cs=("AIZ321",))
        query.guess_ends(r)
        self.assertIsNone(r["arr"])

    def test_schedule_segment_of_multistop_route(self):
        # WZZ5618 LLBG-LHBP-UBBB: a track Budapest -> Caspian belongs to the second segment
        r = leg("471f00", 0, 9000, [[0, 47.43, 19.26, -1, 0], [9000, 41.0, 48.0, 31000, 450]], dep=ap("LHBP"), cs=("WZZ5618",))
        query.guess_ends(r)
        self.assertEqual(r["arr"]["code"], "UBBB")

    def test_itineraries_and_round_trip(self):
        out = leg("aa", 0, 100, [[0, 32.0, 34.88, -1, 0], [100, 40.9, 47.5, 31000, 450]],
                  dep=ap("LLBG"), arr=ap("UBBB", schedule="X"), countries=[["IL", 0, 10], ["AZ", 90, 100]])
        back = leg("aa", 3600, 9000, [[3600, 40.6, 48.5, 30000, 450], [9000, 32.0, 34.88, -1, 0]],
                   dep=ap("UBBB", schedule="Y"), arr=ap("LLBG"), countries=[["AZ", 3600, 3700], ["IL", 8900, 9000]])
        its = query.itineraries([back, out])
        self.assertEqual(len(its), 1)                                # joined at UBBB although the track was lost far away
        self.assertEqual(query.itin_ccs(its[0]), ["IL", "AZ", "IL"])
        # the round trip contains IL -> AZ
        self.assertTrue(query.match_trip(its[0][:1], {"IL"}, [], {"AZ"}))
        self.assertFalse(query.match_trip(its[0], {"IL"}, [], {"AZ"}))

    def test_from_to_need_airports_unless_loose(self):
        over = leg("bb", 0, 100, [[0, 32.0, 34.0, 33000, 450], [100, 41.0, 48.0, 33000, 450]],
                   countries=[["IL", 0, 10], ["AZ", 90, 100]])
        self.assertFalse(query.match_trip([over], {"IL"}, [], {"AZ"}))
        self.assertTrue(query.match_trip([over], {"IL"}, [], {"AZ"}, loose=True))

    def test_path_ccs_adds_airport_countries(self):
        r = leg("cc", 0, 1, [[0, 0, 0, -1, 0], [1, 0, 0, -1, 0]], dep=ap("LCLK"), arr=ap("LLBG"), countries=[["CY", 0, 0]])
        self.assertEqual(query.path_ccs(r), ["CY", "IL"])

    def test_load_and_country_command(self):
        d = Path(TMP) / "days"
        d.mkdir(parents=True, exist_ok=True)
        rows = [leg("ae0001", 100, 200, [[100, 49.9, 6.7, -1, 0], [200, 32.0, 34.88, -1, 0]], dep=None, arr=None, cls="mil",
                    reg_cc="US", cs=("RCH1", "@@@@@@@@"), countries=[["DE", 100, 110], ["IL", 190, 200]])]
        with gzip.open(d / "2000-01-02.jsonl.gz", "wt") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        got = list(query.load("2000-01-02", None))
        self.assertEqual(got[0]["cs"], ["RCH1"])                     # empty callsign dropped
        self.assertEqual(got[0]["arr"]["code"], "LLBG")              # ground end next to the airport -> probable
        import argparse
        import contextlib
        import io
        a = argparse.Namespace(cc="IL", date="2000-01-02", end=None, cls="mil", reg_cc=None, limit=5, map=None, json=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            query.cmd_country(a)
        self.assertIn("legs touching IL: 1", buf.getvalue())
        self.assertIn("landed in IL: 1", buf.getvalue())


class TestRelations(unittest.TestCase):
    def test_mirror_signal_grouped(self):
        t = {"yearly": [{"year": 2022, "a_X": 150e6, "b_M": 30e6, "b_X": 1.6e9, "a_M": 3e6},
                        {"year": 2023, "a_X": 180e6, "b_M": 40e6, "b_X": 1.4e9, "a_M": 10e6}]}
        s = relations.trade_signals("IL", "AZ", t)
        mirror = [x for x in s if "reports" in x]
        self.assertEqual(len(mirror), 2)                             # one line per direction, years grouped
        self.assertTrue(any(x.startswith("2022–2023: AZ reports $1.4 bn–$1.6 bn") for x in mirror))

    def test_missing_side_and_noise(self):
        t = {"yearly": [{"year": 2024, "b_X": 1e6}, {"year": 2025, "b_X": 120e6, "b_M": 6e6}]}
        s = relations.trade_signals("SY", "AZ", t)
        self.assertEqual(sum("reported no trade" in x for x in s), 1)   # $1 M in 2024 is noise
        self.assertTrue(any("up from under $20 M" in x for x in s))
        self.assertEqual(relations.trade_signals("A", "B", {"yearly": [{"year": 2020}]}),
                         ["no trade reported between A and B by either side in these years"])

    def test_trade_pair_mapping(self):
        T = {"yearly": {2024: {("IL", "AZ", "X"): 5.0, ("AZ", "IL", "M"): 4.0}}, "monthly": {}, "top": {}}
        p = relations.trade_pair("IL", "AZ", T, [2024], [])
        self.assertEqual(p["yearly"][0], {"year": 2024, "a_X": 5.0, "b_M": 4.0})


class TestPages(unittest.TestCase):
    def test_map_and_report_render(self):
        r = leg("738488", 0, 9000, [[0, 32.0, 34.88, -1, 0], [9000, 40.46, 50.04, -1, 0]], dep=ap("LLBG"),
                arr=ap("UBBB", schedule="AIZ321"), countries=[["IL", 0, 1]])
        out = Path(TMP) / "m.html"
        mapview.write(str(out), [r], "t <x>", query.path_ccs, {"IL": "Israel"})
        h = out.read_text()
        self.assertIn('"reg": "738488"', h)
        self.assertIn("t &lt;x&gt;", h)                               # title escaped
        rep = {"countries": {"IL": {"name": "Israel", "iso3": "ISR"}, "AZ": {"name": "Azerbaijan", "iso3": "AZE"}},
               "generated": "2000-01-01T00:00+00:00",
               "pairs": {"IL-AZ": {"a": "IL", "b": "AZ", "signals": ["s <b>"], "trade": {
                   "yearly": [{"year": 2024, "a_X": 1e8}], "monthly": [{"month": "2024-01", "a_X": 1e7}],
                   "top": {"a_X": {"year": 2024, "items": [{"hs": "88", "name": "Aircraft", "usd": 2e8}]}}}}},
               "shipping": {"IL": {"monthly": [], "ports_30d": [], "port_links": {}}},
               "flights": {"span": "x", "pairs": {"IL-AZ": {"direct": 1, "inferred": 2, "direct_mil_state": 0, "examples": []},
                                                  "AZ-IL": {"direct": 0, "inferred": 0, "direct_mil_state": 0, "examples": []}},
                           "state_visits": {"IL-in-AZ": [], "AZ-in-IL": []}, "overflights": {"IL-over-AZ": 3, "AZ-over-IL": 0}}}
        out = Path(TMP) / "r.html"
        report_page.write(out, rep, True)
        h = out.read_text()
        self.assertIn("s &lt;b&gt;", h)
        self.assertIn("publishes no daily port figures", h)
        self.assertIn("flights.html", h)

    def test_flag(self):
        self.assertEqual(mapview.flag("IL"), "\U0001F1EE\U0001F1F1")
        self.assertEqual(mapview.flag("CYN"), "")


class TestLive(unittest.TestCase):
    def test_route_for(self):
        import live
        live._AP, live._RT = FakeAirports(), FakeRoutes()
        r = live.route_for("WZZ5618", 44.0, 35.0)                   # between Budapest and Baku
        self.assertEqual((r["from"]["code"], r["to"]["code"]), ("LHBP", "UBBB"))
        r = live.route_for("WZZ5618", 40.0, 27.0)                   # nearer the first segment's line
        self.assertEqual((r["from"]["code"], r["to"]["code"]), ("LLBG", "LHBP"))
        self.assertIsNone(live.route_for("XYZ1", -30.0, 150.0))     # far off: callsign reused, no route
        self.assertIsNone(live.route_for("", 0, 0))

    def test_ship_kind(self):
        import live
        self.assertEqual(live.ship_kind(80), "tanker")
        self.assertEqual(live.ship_kind(35), "military")
        self.assertEqual(live.ship_kind(None), "other")


if __name__ == "__main__":
    unittest.main(verbosity=1)
