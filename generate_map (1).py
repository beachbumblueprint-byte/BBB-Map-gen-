#!/usr/bin/env python3
"""
Beach Bum Blueprint — Country Map Generator (Simplified Layout v2)
====================================================================
Usage:
    python generate_map.py "Costa Rica"
    python generate_map.py "Vietnam" --pins beach_pins.csv

Layout (locked, six elements only — no clutter):
  1. Flag (fit into a fixed box, never stretched/distorted)
  2. Country name (auto-sized to always fit, long or short)
  3. One combined "where in the world" locator — shaded hemisphere +
     country highlighted, answers both "which half of the globe" and
     "exactly where" in a single image
  4. Short facts strip: Capital, Language, Currency, Climate
     (climate is auto-calculated from latitude — nothing to type)
  5. Big main map with numbered beach pins (shape never distorted,
     regardless of whether the country is tall/thin like Chile or
     wide like Brazil — extra background is added on the short side
     instead of squishing the country)
  6. Footer: tagline + website

The ONLY thing you provide per country: the list of featured beaches
in beach_pins.csv. Everything else — flag, facts, climate, hemisphere,
map shape — is fully automatic.

Requires internet access to run (country facts API, geocoding API,
boundary data, flag CDN) — run this inside Claude Code (web or
terminal), not in a sandbox without internet.
"""

import argparse
import csv
import io
import os
import subprocess
import sys
import time
import textwrap

# ---------------------------------------------------------------------
# SELF-INSTALLING — this is the ONLY file you need. If the required
# packages aren't installed yet, this installs them automatically the
# first time you run the script. No separate requirements.txt needed.
# ---------------------------------------------------------------------
REQUIRED_PACKAGES = ["requests", "geopandas", "matplotlib", "Pillow", "shapely", "pyproj", "fiona"]


def ensure_packages_installed():
    try:
        import requests  # noqa
        import geopandas  # noqa
        import matplotlib  # noqa
        import PIL  # noqa
        return
    except ImportError:
        print("First run — installing required packages, this takes a minute...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *REQUIRED_PACKAGES])
        print("Packages installed.")


ensure_packages_installed()

import requests
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------
# BRAND CONSTANTS — locked BBB style. Change once here, applies to
# every country map generated from now on.
# ---------------------------------------------------------------------
BRAND = {
    "navy_header": (13, 42, 66),
    "ocean_blue": (27, 110, 140),
    "palm_green": (62, 142, 90),
    "aqua": (44, 184, 174),
    "brick_red": (192, 59, 43),
    "sand": (245, 240, 224),
    "white": (255, 255, 255),
    "text_dark": (30, 30, 30),
    "hemisphere_shade": (27, 110, 140, 60),  # translucent overlay
}

CANVAS_W, CANVAS_H = 2400, 1600
HEADER_H = 90
FOOTER_H = 70
TOP_STRIP_H = 340          # flag + name + facts strip, full width
LOCATOR_W = 420            # locator box, top-right corner of top strip
FLAG_BOX_W, FLAG_BOX_H = 300, 190

TAGLINE = "Live Well.  Retire Happy.  Life's Better by the Beach."
WEBSITE = "www.beachbumblueprint.com"

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

NOMINATIM_HEADERS = {
    "User-Agent": "BeachBumBlueprintMapGenerator/1.0 (beachbumblueprint@gmail.com)"
}

WORLD_BOUNDARIES_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_50m_admin_0_countries.geojson"
)


# ---------------------------------------------------------------------
# STEP 1 — Country facts (capital, language, currency — climate is
# calculated, not looked up, since it never meaningfully changes)
# ---------------------------------------------------------------------
def climate_from_latitude(lat: float) -> str:
    a = abs(lat)
    if a <= 10:
        return "Equatorial / Tropical"
    elif a <= 23.5:
        return "Tropical"
    elif a <= 35:
        return "Subtropical"
    elif a <= 50:
        return "Temperate"
    else:
        return "Cold / Continental"


def get_country_facts(country_name: str) -> dict:
    url = f"https://restcountries.com/v3.1/name/{country_name}?fullText=true"
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    data = resp.json()[0]

    capital = data.get("capital", ["—"])[0]
    languages = ", ".join(data.get("languages", {}).values()) or "—"
    currencies = data.get("currencies", {})
    if currencies:
        cur = list(currencies.values())[0]
        currency = f"{cur.get('name')} ({cur.get('symbol', '')})"
    else:
        currency = "—"
    cca2 = data.get("cca2", "")
    latlng = data.get("latlng", [0, 0])
    official_name = data.get("name", {}).get("common", country_name)
    climate = climate_from_latitude(latlng[0])
    hemisphere_ns = "Northern Hemisphere" if latlng[0] >= 0 else "Southern Hemisphere"
    hemisphere_ew = "Western Hemisphere" if latlng[1] < 0 else "Eastern Hemisphere"

    return {
        "name": official_name,
        "capital": capital,
        "language": languages,
        "currency": currency,
        "cca2": cca2,
        "latlng": latlng,
        "climate": climate,
        "hemisphere": f"{hemisphere_ns}, {hemisphere_ew}",
    }


def download_flag(cca2: str) -> Image.Image:
    url = f"https://flagcdn.com/w320/{cca2.lower()}.png"
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content)).convert("RGBA")


def fit_image_in_box(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """Resizes an image to FIT inside box_w x box_h, preserving its
    aspect ratio (never stretches/distorts) — pastes it centered on a
    box_w x box_h transparent canvas so every country's flag sits in
    an identically-sized, identically-positioned frame."""
    img_ratio = img.width / img.height
    box_ratio = box_w / box_h
    if img_ratio > box_ratio:
        new_w = box_w
        new_h = int(box_w / img_ratio)
    else:
        new_h = box_h
        new_w = int(box_h * img_ratio)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    canvas.paste(resized, ((box_w - new_w) // 2, (box_h - new_h) // 2), resized)
    return canvas


# ---------------------------------------------------------------------
# STEP 2 — Country boundary (real map shape, not a guess)
# ---------------------------------------------------------------------
def load_world_boundaries() -> gpd.GeoDataFrame:
    cache_path = os.path.join(CACHE_DIR, "world_boundaries.geojson")
    if not os.path.exists(cache_path):
        print("Downloading world boundary dataset (one-time, ~5MB)...")
        resp = requests.get(WORLD_BOUNDARIES_URL, timeout=60)
        resp.raise_for_status()
        with open(cache_path, "wb") as f:
            f.write(resp.content)
    return gpd.read_file(cache_path)


def get_country_geometry(world: gpd.GeoDataFrame, country_name: str):
    name_cols = [c for c in ["NAME", "NAME_LONG", "ADMIN", "SOVEREIGNT"] if c in world.columns]
    for col in name_cols:
        match = world[world[col].str.lower() == country_name.lower()]
        if not match.empty:
            return match.iloc[0]
    for col in name_cols:
        match = world[world[col].str.lower().str.contains(country_name.lower(), na=False)]
        if not match.empty:
            return match.iloc[0]
    raise ValueError(f"Could not find '{country_name}' in the boundary dataset.")


# ---------------------------------------------------------------------
# STEP 3 — Geocode the beach pins (fixes pins landing in the ocean)
# ---------------------------------------------------------------------
def geocode_place(query: str) -> tuple:
    cache_path = os.path.join(CACHE_DIR, "geocode_cache.csv")
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) == 3:
                    cache[row[0]] = (float(row[1]), float(row[2]))
    if query in cache:
        return cache[query]

    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": query, "format": "json", "limit": 1}
    resp = requests.get(url, params=params, headers=NOMINATIM_HEADERS, timeout=20)
    resp.raise_for_status()
    results = resp.json()
    time.sleep(1.1)  # required: max ~1 request/sec to Nominatim

    if not results:
        raise ValueError(
            f"Could not geocode '{query}'. Try adding more detail, e.g. "
            f"'{query}, [region name]' instead of just the town name."
        )
    lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
    with open(cache_path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([query, lat, lon])
    return lat, lon


DEFAULT_PINS_CSV = """country,pin_number,beach_name,geocode_query
Costa Rica,1,Tamarindo,"Tamarindo, Guanacaste, Costa Rica"
Costa Rica,2,Manuel Antonio Beach,"Manuel Antonio, Puntarenas, Costa Rica"
Costa Rica,3,Jaco Beach,"Jaco, Puntarenas, Costa Rica"
Costa Rica,4,Puerto Viejo de Talamanca,"Puerto Viejo de Talamanca, Limon, Costa Rica"
Costa Rica,5,Santa Teresa Beach,"Santa Teresa, Puntarenas, Costa Rica"
"""


def ensure_pins_file_exists(csv_path: str):
    """Creates beach_pins.csv with a starter example if it doesn't exist
    yet — so there's nothing extra to upload separately."""
    if not os.path.exists(csv_path):
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_PINS_CSV)
        print(f"Created {csv_path} with a starter example (Costa Rica). "
              f"Edit it to add rows for your own countries.")


def load_beach_pins(csv_path: str, country_name: str) -> list:
    """CSV columns: country, pin_number, beach_name, geocode_query(optional)"""
    ensure_pins_file_exists(csv_path)
    pins = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["country"].strip().lower() != country_name.lower():
                continue
            query = row.get("geocode_query", "").strip() or f"{row['beach_name']}, {country_name}"
            lat, lon = geocode_place(query)
            pins.append({
                "number": row["pin_number"],
                "name": row["beach_name"],
                "lat": lat,
                "lon": lon,
            })
    pins.sort(key=lambda p: p["number"])
    return pins


# ---------------------------------------------------------------------
# STEP 4 — Draw the main map. Shape is NEVER distorted: extra padding
# is added on whichever side is short so the country always fills the
# frame without being squished (fixes the Chile/Brazil problem).
# ---------------------------------------------------------------------
def compute_padded_extent(minx, miny, maxx, maxy, target_aspect, base_pad_frac=0.15):
    width = maxx - minx
    height = maxy - miny
    pad_x = width * base_pad_frac
    pad_y = height * base_pad_frac
    width += 2 * pad_x
    height += 2 * pad_y
    cur_aspect = width / height
    if cur_aspect < target_aspect:
        new_width = height * target_aspect
        extra = (new_width - width) / 2
        pad_x += extra
    else:
        new_height = width / target_aspect
        extra = (new_height - height) / 2
        pad_y += extra
    return minx - pad_x, miny - pad_y, maxx + pad_x, maxy + pad_y


def draw_main_map(world, country_row, pins, out_path, target_w_px, target_h_px):
    dpi = 150
    fig, ax = plt.subplots(figsize=(target_w_px / dpi, target_h_px / dpi), dpi=dpi)
    ax.set_facecolor((190 / 255, 220 / 255, 235 / 255))
    ax.set_aspect("equal")  # preserves true shape — no stretching

    minx, miny, maxx, maxy = country_row.geometry.bounds
    target_aspect = target_w_px / target_h_px
    minx, miny, maxx, maxy = compute_padded_extent(minx, miny, maxx, maxy, target_aspect)

    world.plot(ax=ax, color="#e8e4d8", edgecolor="#b0aa96", linewidth=0.5)
    gpd.GeoSeries([country_row.geometry]).plot(
        ax=ax, color="#cfe3c2", edgecolor="#5a6b52", linewidth=1.2
    )

    for pin in pins:
        ax.plot(pin["lon"], pin["lat"], "o", markersize=22,
                 color="#" + "%02x%02x%02x" % BRAND["brick_red"], zorder=5)
        ax.text(pin["lon"], pin["lat"], str(pin["number"]),
                 color="white", fontsize=11, fontweight="bold",
                 ha="center", va="center", zorder=6)

    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    fig.savefig(out_path, dpi=dpi)
    plt.close(fig)


def draw_hemisphere_locator(world, country_row, latlng, out_path, box_px):
    """One combined locator: shades the country's hemisphere and
    highlights the country itself — answers 'which half of the globe'
    and 'exactly where' in a single image."""
    dpi = 150
    fig, ax = plt.subplots(figsize=(box_px / dpi, box_px / dpi), dpi=dpi)
    ax.set_facecolor((190 / 255, 220 / 255, 235 / 255))
    ax.set_aspect("equal")

    world.plot(ax=ax, color="#e8e4d8", edgecolor="#b0aa96", linewidth=0.3)

    # Shade the hemisphere the country sits in
    lat = latlng[0]
    if lat >= 0:
        ax.axhspan(0, 90, color="#" + "%02x%02x%02x" % BRAND["ocean_blue"], alpha=0.15)
    else:
        ax.axhspan(-90, 0, color="#" + "%02x%02x%02x" % BRAND["ocean_blue"], alpha=0.15)

    # Highlight the country
    gpd.GeoSeries([country_row.geometry]).plot(
        ax=ax, color="#" + "%02x%02x%02x" % BRAND["brick_red"]
    )

    ax.axhline(0, color="#555555", linewidth=0.6, linestyle="--")  # equator line
    ax.set_xlim(-180, 180)
    ax.set_ylim(-60, 85)
    ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    fig.savefig(out_path, dpi=dpi)
    plt.close(fig)


# ---------------------------------------------------------------------
# STEP 5 — Compose the final branded poster (simplified layout)
# ---------------------------------------------------------------------
def load_font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def autosize_font(draw, text, max_width, start_size, bold=True, min_size=28):
    size = start_size
    while size > min_size:
        font = load_font(size, bold=bold)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 4
    return load_font(min_size, bold=bold)


def rgb(name):
    return BRAND[name][:3]


def compose_poster(country_name, facts, pins, main_map_path, locator_path, out_path):
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), rgb("white"))
    draw = ImageDraw.Draw(canvas)

    # ---- Header bar ----
    draw.rectangle([0, 0, CANVAS_W, HEADER_H], fill=rgb("navy_header"))
    f_header = load_font(34, bold=True)
    f_header_small = load_font(24)
    draw.text((30, 25), "BEACH BUM BLUEPRINT MAP SERIES", font=f_header, fill=rgb("white"))
    label = "COUNTRY MAP TEMPLATE"
    w = draw.textlength(label, font=f_header_small)
    draw.text((CANVAS_W - w - 30, 32), label, font=f_header_small, fill=rgb("white"))

    # ---- Top strip: flag, name, facts (left) + locator (right) ----
    strip_y0 = HEADER_H
    strip_y1 = HEADER_H + TOP_STRIP_H
    draw.rectangle([0, strip_y0, CANVAS_W, strip_y1], fill=rgb("sand"))

    # Flag — fit, never stretched, uniform frame
    flag_raw = download_flag(facts["cca2"])
    flag_fitted = fit_image_in_box(flag_raw, FLAG_BOX_W, FLAG_BOX_H)
    flag_x, flag_y = 40, strip_y0 + 30
    draw.rectangle([flag_x - 4, flag_y - 4, flag_x + FLAG_BOX_W + 4, flag_y + FLAG_BOX_H + 4],
                    outline=rgb("ocean_blue"), width=3)
    canvas.paste(flag_fitted, (flag_x, flag_y), flag_fitted)

    # Country name — auto-sized so long names never overflow
    name_x = flag_x + FLAG_BOX_W + 40
    name_max_w = CANVAS_W - LOCATOR_W - name_x - 40
    f_title = autosize_font(draw, country_name.upper(), name_max_w, start_size=72, min_size=32)
    draw.text((name_x, strip_y0 + 40), country_name.upper(), font=f_title, fill=rgb("text_dark"))

    # Facts strip — Capital / Language / Currency / Climate only
    f_label = load_font(22, bold=True)
    f_value = load_font(22)
    facts_y = strip_y0 + 130
    facts_list = [
        ("Capital", facts["capital"]),
        ("Language", facts["language"]),
        ("Currency", facts["currency"]),
        ("Climate", facts["climate"]),
    ]
    col_w = name_max_w // 2
    for i, (label_text, value_text) in enumerate(facts_list):
        col = i % 2
        row = i // 2
        fx = name_x + col * col_w
        fy = facts_y + row * 70
        draw.text((fx, fy), f"{label_text}:", font=f_label, fill=rgb("ocean_blue"))
        wrapped = textwrap.shorten(value_text, width=30, placeholder="...")
        draw.text((fx, fy + 30), wrapped, font=f_value, fill=rgb("text_dark"))

    # Locator — top right corner of the strip
    loc_x = CANVAS_W - LOCATOR_W - 20
    loc_y = strip_y0 + 20
    loc_img = Image.open(locator_path).resize((LOCATOR_W, LOCATOR_W))
    canvas.paste(loc_img, (loc_x, loc_y))
    draw.rectangle([loc_x, loc_y, loc_x + LOCATOR_W, loc_y + LOCATOR_W],
                    outline=rgb("ocean_blue"), width=3)
    draw.text((loc_x, loc_y - 26), "WHERE IN THE WORLD", font=load_font(18, bold=True), fill=rgb("ocean_blue"))

    # ---- Main map (fills remaining space below the strip) ----
    map_y0 = strip_y1
    map_y1 = CANVAS_H - FOOTER_H
    main_img = Image.open(main_map_path)
    canvas.paste(main_img, (0, map_y0))

    # Beach pin key, bottom-left overlay on the map
    key_x, key_y = 30, map_y0 + 20
    f_pin_num = load_font(16, bold=True)
    f_pin_name = load_font(18)
    for pin in pins:
        cx, cy, r = key_x + 12, key_y + 12, 13
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=rgb("brick_red"), outline=rgb("white"), width=2)
        num_w = draw.textlength(str(pin["number"]), font=f_pin_num)
        draw.text((cx - num_w / 2, cy - 9), str(pin["number"]), font=f_pin_num, fill=rgb("white"))
        draw.text((key_x + 34, key_y + 2), pin["name"], font=f_pin_name, fill=rgb("text_dark"))
        key_y += 32

    # ---- Footer bar ----
    draw.rectangle([0, CANVAS_H - FOOTER_H, CANVAS_W, CANVAS_H], fill=rgb("navy_header"))
    f_footer = load_font(20, bold=True)
    draw.text((30, CANVAS_H - FOOTER_H + 22), TAGLINE, font=load_font(16), fill=rgb("white"))
    w = draw.textlength(WEBSITE, font=f_footer)
    draw.text((CANVAS_W - w - 30, CANVAS_H - FOOTER_H + 22), WEBSITE, font=f_footer, fill=rgb("white"))

    canvas.save(out_path, "PNG")


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Generate a BBB branded country map.")
    parser.add_argument("country", help="Country name, e.g. 'Costa Rica'")
    parser.add_argument("--pins", default="beach_pins.csv", help="Path to beach pins CSV")
    parser.add_argument("--out", default=None, help="Output PNG path")
    args = parser.parse_args()

    country_name = args.country
    out_path = args.out or f"{country_name.replace(' ', '_')}_map.png"
    tmp_dir = os.path.join(CACHE_DIR, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    print(f"1/5  Looking up facts for {country_name}...")
    facts = get_country_facts(country_name)

    print("2/5  Loading world boundary data...")
    world = load_world_boundaries()
    country_row = get_country_geometry(world, country_name)

    print("3/5  Geocoding featured beach pins...")
    pins = load_beach_pins(args.pins, country_name)
    if not pins:
        print(f"  No pins found for '{country_name}' in {args.pins} — "
              f"add rows there first (see beach_pins_template.csv).")

    print("4/5  Drawing maps...")
    main_map_path = os.path.join(tmp_dir, "main_map.png")
    locator_path = os.path.join(tmp_dir, "locator.png")
    main_map_w = CANVAS_W
    main_map_h = CANVAS_H - HEADER_H - TOP_STRIP_H - FOOTER_H
    draw_main_map(world, country_row, pins, main_map_path, main_map_w, main_map_h)
    draw_hemisphere_locator(world, country_row, facts["latlng"], locator_path, LOCATOR_W)

    print("5/5  Composing final poster...")
    compose_poster(facts["name"], facts, pins, main_map_path, locator_path, out_path)

    print(f"Done: {out_path}")


if __name__ == "__main__":
    main()
