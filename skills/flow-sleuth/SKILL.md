---
name: flow-sleuth
description: Open-source intelligence on what moves between countries. Which military, state or civil aircraft flew into, out of or over a country on given days, which airports they used, where an aircraft went; flights, trade, shipping and port activity between two or more countries; live aircraft, ships and trains on a map. Use for questions like "which military aircraft landed in Israel yesterday", "did any aircraft fly from Israel to Azerbaijan last week and over which countries", "how is trade between X and Y changing", "is anything flying over Syria now".
---

# flow-sleuth (v0.1)

> `${CLAUDE_SKILL_DIR}` below is the directory containing this SKILL.md. Every script runs with `uv run`; no keys.

## Turn the question into commands

| Question | Command |
|---|---|
| Which military / state aircraft were in or over country X on day D? Where did they land? | `day.py D` then `query.py country X --date D --class mil,state --map out/x.html` |
| All traffic of X (landings by airport, routes) | `query.py country X --date D [--end D2]` |
| Did aircraft go from A to C via B? (also with stops) | `query.py route --from A --via B --to C --date D [--end D2] [--class ...] [--reg-cc R] [--max-stops 1] [--loose]` — from/to are take-off/landing airport countries; `--loose` also accepts a track merely lost over the country (military landings are often not heard) |
| Aircraft registered in R over / into X | `query.py route --reg-cc R --via X --date D` |
| Where did aircraft TC-XXX (or ICAO hex) go? | `query.py aircraft TC-XXX --date D --end D2` |
| How well is a country covered (can an absence mean anything)? | `query.py coverage --date D --cc X,Y` |
| Relations between countries: trade, shipping, flights | `relations.py A B [C ...] --flights D1:D2 --out out/a-b` (opens `index.html`) |
| Live map (aircraft anywhere, ships and trains where open) | `live.py [--at lat,lon --nm 250]` |

Steps:
1. Dates are UTC days. `day.py <date> [<end>]` downloads each day once (about 4 GB streamed, 5–7 minutes per day, ~190 MB cached in `~/.cache/flow-sleuth/days/`). Data for a day appears the next morning UTC; for "today" use `live.py`.
2. Country codes are ISO 3166 alpha-2 (IL, AZ, SY, GR, CY, CN …).
3. Run `query.py coverage` for the countries in the question whenever the answer is "none" or a small number.
4. Give the answer in the user's language: a short table (time UTC, registration, type and its full name, callsign, operator, state of registry, from → to, countries crossed), then the map path, then the limits that apply.

## What the outputs mean

- `[mil]`: the aircraft database (readsb / tar1090-db) flags it military. Includes government VIP fleets that are run by air forces (e.g. "Qatar Amiri Flight", "Bahrain Royal Flight").
- `[state]`: operator name looks governmental (government, royal, air force, police …) but no military flag.
- `registry` = state the ICAO 24-bit address block belongs to = where the aircraft is registered. Not the operator's nationality, not who chartered it.
- Airport marks: no mark = take-off/landing heard at the airport; `~` = probable (track lost below 12,000 ft within 40 km of the airport); `*` = from the callsign's timetable (VRS standing data), i.e. planned, not observed.
- `first/last seen` = track starts or ends in the air: receivers lost it; the true origin/destination is unknown.
- Countries crossed use land borders (Natural Earth), not airspace boundaries.

## Limits to state in every answer that depends on them

- **No passengers, ever.** Data shows aircraft, not who is on board. Never infer a person's travel from an aircraft movement.
- **Dark flights.** Many military flights switch ADS-B off or are not in the database; absence in the data is not absence in the sky.
- **Coverage.** Receivers are volunteers'. Europe, Israel and the US are well covered; near the ground Syria, Iraq, Azerbaijan and much of China poorly or not at all. Use `coverage`.
- **Privacy.** Aircraft flagged PIA or LADD (owners asked not to be tracked) are dropped at ingest and never shown. Do not try to work around this, and do not build profiles of private individuals' aircraft.
- **Trade** is what each government reports to UN Comtrade; months arrive 2–4 months late; a country can withhold a partner. Show both sides' figures rather than picking one.
- **Shipping**: port calls and tonnage are measured from AIS by IMF PortWatch; port-to-country trade values are IMF's model. No vessel-level tracks outside Finland (live) are used.
- **Relations** are reported as measurable signals (trade up/down, reporting gaps, direct flights or none, state aircraft visits), never as a verdict on whether relations are good or bad.

## Files

- `scripts/day.py` – one UTC day of adsb.lol history → flight legs (airports, countries crossed, simplified track).
- `scripts/query.py` – country / route / aircraft / coverage questions; `--map` writes an interactive map (list of flights with type, callsign, operator, route; click to highlight).
- `scripts/relations.py` + `report_page.py` – multi-country report page.
- `scripts/live.py` + `web/live.html` – live map server; `--snapshot` for a static site.
- `data/icao_blocks.json` (ICAO address allocations), `data/countries.geojson` (Natural Earth 1:50m).
