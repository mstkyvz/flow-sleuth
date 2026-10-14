# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "shapely>=2", "pillow"]
# ///
"""Replay a day of flights as a GIF on an OpenStreetMap basemap: aircraft move along their tracks with a fading
trail, labelled with registration and type; a clock and a running list.

  uv run docs/make_replay_gif.py country IL --date 2026-10-12 --class mil,state docs/img/il.gif --zoom 7 --bbox 29.3,32.2,34.2,37.2
  uv run docs/make_replay_gif.py between IL,AZ --date 2026-10-12 docs/img/il_az.gif
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import math
import sys
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "flow-sleuth" / "scripts"))
from common import CACHE  # noqa: E402
from mapview import PALETTE  # noqa: E402
from query import load, path_ccs  # noqa: E402

UA = {"User-Agent": "flow-sleuth docs (https://github.com/mstkyvz/flow-sleuth)"}


def font(n, bold=False):
    for p in (f"/System/Library/Fonts/Supplemental/Arial{' Bold' if bold else ''}.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(p, n)
        except OSError:
            pass
    return ImageFont.load_default()


def merc(lat, lon, z):
    n = 256 * 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def tile(z, x, y):
    f = CACHE / "tiles" / f"{z}_{x}_{y}.png"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(urllib.request.Request(f"https://tile.openstreetmap.org/{z}/{x}/{y}.png", headers=UA), timeout=60) as r:
            f.write_bytes(r.read())
        time.sleep(0.2)
    return Image.open(f).convert("RGB")


def basemap(s, w, n, e, z):
    x0, y0 = merc(n, w, z)
    x1, y1 = merc(s, e, z)
    im = Image.new("RGB", (int(x1 - x0), int(y1 - y0)))
    for tx in range(int(x0 // 256), int(x1 // 256) + 1):
        for ty in range(int(y0 // 256), int(y1 // 256) + 1):
            im.paste(tile(z, tx % 2 ** z, ty), (int(tx * 256 - x0), int(ty * 256 - y0)))
    # wash out so tracks stand out
    return Image.blend(im, Image.new("RGB", im.size, (250, 250, 248)), 0.45), (x0, y0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["country", "between"])
    ap.add_argument("what")
    ap.add_argument("out")
    ap.add_argument("--date", required=True)
    ap.add_argument("--class", dest="cls")
    ap.add_argument("--zoom", type=int, default=5)
    ap.add_argument("--step", type=int, default=20, help="minutes per frame")
    ap.add_argument("--title")
    ap.add_argument("--bbox", help="s,w,n,e: map extent (default: all tracks); tracks are clipped to it")
    a = ap.parse_args()
    classes = set(a.cls.split(",")) if a.cls else None
    ccs = [c.upper() for c in a.what.split(",")]
    legs = []
    for r in load(a.date, None, need_pts=True):
        if classes and r["cls"] not in classes:
            continue
        seq = path_ccs(r)
        if a.mode == "country" and ccs[0] in seq:
            legs.append(r)
        elif a.mode == "between":
            s, e = (r["dep"] or {}).get("cc"), (r["arr"] or {}).get("cc")
            if s in ccs and e in ccs and s != e:
                legs.append(r)
    if not legs:
        raise SystemExit("no flights")
    lats = [p[1] for r in legs for p in r["pts"]]
    lons = [p[2] for r in legs for p in r["pts"]]
    pad = 1.5
    s, n, w, e = min(lats) - pad, max(lats) + pad, min(lons) - pad, max(lons) + pad
    if a.bbox:
        s, w, n, e = map(float, a.bbox.split(","))
    z = a.zoom
    base, (ox, oy) = basemap(s, w, n, e, z)
    W = 820
    k = W / base.width
    base = base.resize((W, int(base.height * k)), Image.LANCZOS)
    H = base.height

    def xy(lat, lon):
        x, y = merc(lat, lon, z)
        return (x - ox) * k, (y - oy) * k

    colors = {}
    for r in sorted(legs, key=lambda r: r["t0"]):
        colors.setdefault(r["icao"], PALETTE[len(colors) % len(PALETTE)])
    proj = [(r, [(p[0], *xy(p[1], p[2])) for p in r["pts"]]) for r in legs]
    day0 = int(dt.datetime.fromisoformat(a.date).replace(tzinfo=dt.timezone.utc).timestamp())
    f_big, f_mid, f_sm = font(22, True), font(15, True), font(13)
    panel = 300
    frames = []
    steps = list(range(0, 24 * 60 + 1, a.step))
    for i, mins in enumerate(steps + [steps[-1]] * 12):
        now = day0 + mins * 60
        im = Image.new("RGB", (W + panel, max(H, 560)), (255, 255, 255))
        im.paste(base, (0, 0))
        d = ImageDraw.Draw(im, "RGBA")
        active = []
        for r, pts in proj:
            c = colors[r["icao"]]
            rgb = tuple(int(c[j:j + 2], 16) for j in (1, 3, 5))
            done = [(x, y) for t, x, y in pts if t <= now]
            if len(done) >= 2:
                d.line(done, fill=rgb + (230,), width=3)
            if r["t0"] <= now <= r["t1"] and done and 0 <= done[-1][0] <= W and 0 <= done[-1][1] <= H:
                x, y = done[-1]
                d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=rgb + (255,), outline=(255, 255, 255, 255), width=2)
                lab = f"{r['r'] or r['icao']} {r['t'] or ''}"
                tw = d.textlength(lab, font=f_sm)
                d.rounded_rectangle((x + 8, y - 9, x + 14 + tw, y + 8), 4, fill=(255, 255, 255, 225), outline=rgb + (255,))
                d.text((x + 11, y - 8), lab, fill=(20, 20, 20), font=f_sm)
                active.append(r)
        # side panel
        d.rectangle((W, 0, W + panel, im.height), fill=(250, 250, 248))
        title = a.title or (f"{ccs[0]} · {a.cls or 'all'} aircraft" if a.mode == "country" else "Flights between " + " · ".join(ccs))
        d.text((W + 14, 12), title, fill=(20, 20, 20), font=f_mid)
        d.text((W + 14, 36), dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%d %b %Y  %H:%M UTC"), fill=(20, 20, 20), font=f_big)
        d.text((W + 14, 66), f"in the air now: {len(active)}", fill=(90, 90, 90), font=f_sm)
        y = 92
        for r in active[:16]:
            c = colors[r["icao"]]
            rgb = tuple(int(c[j:j + 2], 16) for j in (1, 3, 5))
            d.rectangle((W + 14, y + 3, W + 24, y + 13), fill=rgb)
            d.text((W + 30, y), f"{r['r'] or r['icao']}  {r['t'] or ''}  {'/'.join(r['cs'][:1])}", fill=(20, 20, 20), font=f_sm)
            fr = (r["dep"] or {}).get("code") or "?"
            to = (r["arr"] or {}).get("code") or "?"
            d.text((W + 30, y + 15), f"{(r['desc'] or '')[:24]}  {fr} → {to}", fill=(100, 100, 100), font=f_sm)
            y += 34
        d.text((8, H - 18), "ADS-B: adsb.lol (ODbL) · map © OpenStreetMap contributors", fill=(60, 60, 60), font=f_sm)
        frames.append(im.quantize(colors=200, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(a.out, save_all=True, append_images=frames[1:], duration=110, loop=0, optimize=True)
    print(f"{a.out}: {len(frames)} frames, {len(legs)} flights, {Path(a.out).stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
