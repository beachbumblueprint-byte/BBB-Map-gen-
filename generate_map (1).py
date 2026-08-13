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
import zlib

# ---------------------------------------------------------------------
# SELF-INSTALLING — this is the ONLY file you need. If the required
# packages aren't installed yet, this installs them automatically the
# first time you run the script. No separate requirements.txt needed.
# ---------------------------------------------------------------------
REQUIRED_PACKAGES = ["requests", "geopandas", "matplotlib", "Pillow", "shapely", "pyproj", "fiona", "numpy"]


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

import numpy as np
import requests
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.offsetbox import OffsetImage, AnnotationBbox
from matplotlib.path import Path as MplPath
from matplotlib.patches import PathPatch
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import box as shp_box, Point
from shapely.ops import transform as shp_transform, unary_union
from shapely.prepared import prep

# ---------------------------------------------------------------------
# BRAND CONSTANTS — locked BBB style. Change once here, applies to
# every country map generated from now on.
# ---------------------------------------------------------------------
BRAND = {
    "navy_header": (13, 42, 66),
    "ocean_blue": (27, 110, 140),
    "ocean_light": (147, 197, 224),  # visibly blue map ocean fill
    "palm_green": (62, 142, 90),
    "aqua": (44, 184, 174),
    "brick_red": (192, 59, 43),
    "sand": (236, 220, 180),        # clearly tan, not a near-white
    "sand_deep": (216, 194, 143),   # darker end of the top-strip gradient
    "white": (255, 255, 255),
    "text_dark": (30, 30, 30),
    "highlight_land": (36, 156, 108),   # a modern emerald green for the
                                         # featured country — still reads as
                                         # "the star" next to the neighbors
    "neighbor_land": (200, 202, 206),  # fallback only, if a country name column
                                        # isn't available to pick a palette color
}

# Each non-featured country gets its own shade from this set (picked
# deterministically per country name) instead of one flat fill, so
# neighboring countries are never the same shade as each other. Narrowed
# down to one cohesive brown/tan family on purpose — a full rainbow of
# unrelated hues was reading as "too many colors to pick"; keeping the
# supporting cast in one earth-tone family lets the featured country's
# green stand out as the one clearly different color on the page, and
# dark navy label text (no outline) stays legible on all of these.
NEIGHBOR_PALETTE = [
    "#B98858",  # warm tan
    "#8B6F47",  # medium brown
    "#C9A66B",  # sandy tan
    "#A67B5B",  # terracotta-brown
    "#D4B483",  # pale gold-tan
    "#7A6142",  # deep umber
]


def neighbor_color_for(name) -> str:
    return NEIGHBOR_PALETTE[zlib.crc32(str(name).encode("utf-8")) % len(NEIGHBOR_PALETTE)]


def adaptive_text_color(bg_hex: str) -> str:
    """Picks dark-navy or white label text depending on the actual
    brightness of the specific background it's sitting on — the
    neighbor palette spans light tans through dark umber, and a single
    fixed text color reads fine on some of those and badly on others."""
    r, g, b = (int(bg_hex.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return hex_of("navy_header") if luminance > 140 else "#ffffff"


CANVAS_W, CANVAS_H = 2400, 1600
HEADER_H = 112             # tall enough for genuinely large corner text —
FOOTER_H = 92               # this is meant to be viewed on a phone screen
TOP_STRIP_H = 420          # flag + name + artwork strip, full width — grown
                            # to give the artwork real room instead of
                            # squeezing it under the title
FLAG_BOX_W, FLAG_BOX_H = 380, 240  # most flags share a similar aspect ratio,
                                    # so a bigger fixed box is safe to standardize on

ARTWORK_PATH = os.path.join(os.path.dirname(__file__), "assets", "bbb_logo.png")
ARTWORK_BOX_W, ARTWORK_BOX_H = 420, 270  # brand artwork under the country name,
                                          # same fixed-box treatment as the flag —
                                          # as big as the strip's remaining height allows

# Locator box, top-right corner of the top strip. Sized to the real
# aspect ratio of its world view (see LOCATOR_LAT_MIN/MAX below) so the
# map fills the box edge-to-edge with no letterboxing, and kept clear
# of the main map paste below it (see compose_poster).
LOCATOR_LAT_MIN, LOCATOR_LAT_MAX = -60, 85
LOCATOR_W = 640
LOCATOR_H = round(LOCATOR_W * (LOCATOR_LAT_MAX - LOCATOR_LAT_MIN) / 360)

MAX_CITY_LABELS = 1  # capital only — keep the map simple

TAGLINE = "Live Well.  Retire Happy.  Life's Better by the Beach."
WEBSITE = "www.beachbumblueprint.com"
CONTACT_EMAIL = "beachbumblueprint@gmail.com"

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

NOMINATIM_HEADERS = {
    "User-Agent": "BeachBumBlueprintMapGenerator/1.0 (beachbumblueprint@gmail.com)"
}

WORLD_BOUNDARIES_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_50m_admin_0_countries.geojson"
)
WORLD_CITIES_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_50m_populated_places.geojson"
)
WORLD_MARINE_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_50m_geography_marine_polys.geojson"
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

    capital_list = data.get("capital") or []
    capital = capital_list[0] if capital_list else "—"
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
    if not cca2:
        raise ValueError("no country code available to look up a flag")
    url = f"https://flagcdn.com/w320/{cca2.lower()}.png"
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content)).convert("RGBA")


def placeholder_flag(box_w: int, box_h: int) -> Image.Image:
    """Blank frame with a note, used when no flag is available for a
    country (e.g. missing/unrecognized ISO code) instead of crashing."""
    canvas = Image.new("RGBA", (box_w, box_h), (255, 255, 255, 255))
    d = ImageDraw.Draw(canvas)
    d.rectangle([0, 0, box_w - 1, box_h - 1], outline=(180, 180, 180), width=2)
    text = "No flag\navailable"
    font = load_font(20)
    d.multiline_text((box_w / 2, box_h / 2), text, font=font, fill=(140, 140, 140),
                      anchor="mm", align="center")
    return canvas


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


def load_world_cities() -> gpd.GeoDataFrame:
    cache_path = os.path.join(CACHE_DIR, "world_cities.geojson")
    if not os.path.exists(cache_path):
        print("Downloading world cities dataset (one-time, ~1MB)...")
        resp = requests.get(WORLD_CITIES_URL, timeout=60)
        resp.raise_for_status()
        with open(cache_path, "wb") as f:
            f.write(resp.content)
    return gpd.read_file(cache_path)


def load_world_marine() -> gpd.GeoDataFrame:
    cache_path = os.path.join(CACHE_DIR, "world_marine.geojson")
    if not os.path.exists(cache_path):
        print("Downloading marine names dataset (one-time, ~1MB)...")
        resp = requests.get(WORLD_MARINE_URL, timeout=60)
        resp.raise_for_status()
        with open(cache_path, "wb") as f:
            f.write(resp.content)
    return gpd.read_file(cache_path)


def get_country_cities(cities: gpd.GeoDataFrame, country_row, country_name: str,
                        max_cities: int = MAX_CITY_LABELS) -> list:
    """Picks the capital (if present) plus the largest other cities to
    label on the main map, using the country name where available and
    falling back to a spatial match against the country's own geometry
    (handles cases where the two datasets spell/scope a name differently)."""
    matches = cities[cities["ADM0NAME"].str.lower() == country_name.lower()] \
        if "ADM0NAME" in cities.columns else cities.iloc[0:0]
    if matches.empty:
        matches = cities[cities.geometry.within(country_row.geometry)]
    if matches.empty:
        return []

    matches = matches.copy()
    matches["_pop"] = matches["POP_MAX"].fillna(0) if "POP_MAX" in matches.columns else 0
    is_capital_col = matches["ADM0CAP"] == 1 if "ADM0CAP" in matches.columns else matches["_pop"] < 0
    matches["_is_capital"] = is_capital_col
    ordered = matches.sort_values(["_is_capital", "_pop"], ascending=[False, False])

    result = []
    for _, row in ordered.head(max_cities).iterrows():
        result.append({
            "name": row.get("NAME") or row.get("NAMEASCII") or "",
            "lat": float(row.geometry.y),
            "lon": float(row.geometry.x),
            "is_capital": bool(row["_is_capital"]),
        })
    return result


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
                "number": int(row["pin_number"]),
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


def fix_dateline_wrap(geometry):
    """Countries that cross the antimeridian (Fiji, Russia's far east,
    the US via the Aleutians) have geometry spanning from ~+180 to
    ~-180, so a naive bounding box comes out ~360 degrees wide instead
    of the country's true (much narrower) extent. When that happens,
    shift the negative-longitude side by +360 so the country renders
    as one contiguous landmass instead of the map zooming out to fit
    the whole globe. Returns (geometry, was_shifted)."""
    minx, _, maxx, _ = geometry.bounds
    if maxx - minx <= 180:
        return geometry, False
    shifted = shp_transform(lambda x, y, z=None: (x + 360 if x < 0 else x, y), geometry)
    return shifted, True


def hex_of(brand_key):
    return "#" + "%02x%02x%02x" % BRAND[brand_key][:3]


def draw_neighbor_labels(ax, world_to_plot, country_idx, view_box):
    """Labels other countries visible in the frame (e.g. Nicaragua and
    Panama around Costa Rica) so the map reads without a separate atlas.
    Only labels countries with a meaningful amount of visible area, and
    places the label inside whatever part of them is actually on screen."""
    name_col = next((c for c in ["NAME", "ADMIN", "SOVEREIGNT"] if c in world_to_plot.columns), None)
    if name_col is None:
        return
    view_area = view_box.area
    candidates = []
    for idx, row in world_to_plot.iterrows():
        if idx == country_idx or row.geometry is None:
            continue
        clipped = row.geometry.intersection(view_box)
        if clipped.is_empty or clipped.area < view_area * 0.0015:
            continue
        candidates.append((clipped.area, row[name_col], clipped.representative_point()))
    candidates.sort(key=lambda c: c[0], reverse=True)
    for _, name, point in candidates[:6]:
        text_color = adaptive_text_color(neighbor_color_for(name))
        ax.text(point.x, point.y, name, color=text_color, fontsize=34,
                 fontweight="bold", ha="center", va="center", zorder=3, clip_on=True)


def draw_ocean_labels(ax, marine, view_box, placed_boxes, dpi, deg_per_px_x, deg_per_px_y, max_labels=3):
    """Labels the major bodies of water visible in the frame (e.g.
    Pacific Ocean, Caribbean Sea) — orients viewers without a separate
    atlas, same idea as the neighbor-country labels but for water.
    Italicized and in black to read as clearly different from land
    labels. Uses the same 'biggest actually-open space' search as the
    country name (find_open_space_point): grid-searches the water
    body's own visible shape for the point farthest from its boundary
    that still clears every already-placed label, shrinking the font
    only if nothing fits — instead of nudging around one starting
    point, which kept landing in mediocre spots instead of the big
    open water areas actually available."""
    if "name" not in marine.columns:
        return
    view_area = view_box.area
    candidates = []
    for _, row in marine.iterrows():
        if row.geometry is None:
            continue
        clipped = row.geometry.intersection(view_box)
        if clipped.is_empty or clipped.area < view_area * 0.01:
            continue
        candidates.append((clipped.area, row["name"], clipped))
    candidates.sort(key=lambda c: c[0], reverse=True)
    placed = 0
    for _, name, geom in candidates:
        if placed >= max_labels:
            break
        point, fontsize, fit_ok = find_open_space_point(geom, name, 38, dpi, deg_per_px_x, deg_per_px_y,
                                                          placed_boxes, grid_n=45, min_size_frac=0.6)
        if not fit_ok:
            continue  # no clean spot for this one — skip it rather than show a cramped/cut-off label
        box = label_footprint(point.x, point.y, name, fontsize, dpi, deg_per_px_x, deg_per_px_y, ha="center")
        placed_boxes.append(box)
        ax.text(point.x, point.y, name, color="black", fontsize=fontsize,
                 fontweight="bold", fontstyle="italic", ha="center", va="center", zorder=2, clip_on=True)
        placed += 1


def boxes_overlap(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return ax0 < bx1 and ax1 > bx0 and ay0 < by1 and ay1 > by0


def label_footprint(lon, lat, text, fontsize, dpi, deg_per_px_x, deg_per_px_y, ha="center"):
    """Estimated bounding box (in data/lon-lat coordinates) a label would
    occupy, used only for collision avoidance between labels — doesn't
    need to be pixel-exact, just close enough to keep text from stacking."""
    w = fontsize * 0.62 * (dpi / 72.0) * len(text) * deg_per_px_x
    h = fontsize * 1.3 * (dpi / 72.0) * deg_per_px_y
    if ha == "left":
        return (lon, lat - h / 2, lon + w, lat + h / 2)
    if ha == "right":
        return (lon - w, lat - h / 2, lon, lat + h / 2)
    return (lon - w / 2, lat - h / 2, lon + w / 2, lat + h / 2)


def find_open_space_point(geometry, text, fontsize, dpi, deg_per_px_x, deg_per_px_y,
                           avoid_boxes=(), grid_n=60, min_size_frac=0.5):
    """Finds a spot inside the shape for `text` at `fontsize`: among grid
    points where the text's own rendered bounding box wouldn't overlap
    any avoid_boxes (exclusion zones around pins/cities) AND the text
    fully fits inside the shape, picks whichever is deepest inside the
    shape (farthest from its own boundary) — the biggest unoccupied
    void. A country's true geometric "widest" point often coincides
    with its main pin cluster (that's usually why the cluster is
    there), so this checks the label's real footprint rather than just
    a point, and shrinks the font if nothing fits until something does."""
    minx, miny, maxx, maxy = geometry.bounds
    boundary = geometry.boundary
    prepared = prep(geometry)

    size = fontsize
    while size >= fontsize * min_size_frac:
        best_clear, best_clear_score = None, -1.0
        best_fits, best_fits_score = None, -1.0  # fits inside the shape, but overlaps another label
        best_any, best_any_score = None, -1.0    # last resort — not even guaranteed to fit
        for i in range(grid_n + 1):
            x = minx + (maxx - minx) * i / grid_n
            for j in range(grid_n + 1):
                y = miny + (maxy - miny) * j / grid_n
                p = Point(x, y)
                if not prepared.contains(p):
                    continue
                bd = boundary.distance(p)
                if bd > best_any_score:
                    best_any_score, best_any = bd, p
                box = label_footprint(x, y, text, size, dpi, deg_per_px_x, deg_per_px_y, ha="center")
                fits_inside = prepared.contains(Point(box[0], box[1])) and prepared.contains(Point(box[2], box[3])) \
                    and prepared.contains(Point(box[0], box[3])) and prepared.contains(Point(box[2], box[1]))
                if fits_inside:
                    if bd > best_fits_score:
                        best_fits_score, best_fits = bd, p
                    if not any(boxes_overlap(box, ab) for ab in avoid_boxes) and bd > best_clear_score:
                        best_clear_score, best_clear = bd, p
        if best_clear is not None:
            return best_clear, size, True
        if best_fits is not None:
            return best_fits, size, True
        size -= 2
    # Nothing at any font size ever fit cleanly inside the shape (a
    # sliver too small/oddly-clipped for the label at any size — e.g. a
    # water body barely clipping the corner of the frame). Fall back to
    # the deepest point, clamped so the box can't run off the shape's
    # own bounds — but flag it as not-really-fitting (fit_ok=False) so
    # a caller that can afford to just skip a bad placement (e.g. a
    # "nice to have" ocean label) knows to do that instead of showing
    # a label crammed into a space too small for it.
    fallback_size = fontsize * min_size_frac
    fallback_pt = best_any if best_any is not None else geometry.representative_point()
    clamped = clamp_point_to_bounds(fallback_pt, text, fallback_size, dpi, deg_per_px_x, deg_per_px_y,
                                     minx, miny, maxx, maxy)
    return clamped, fallback_size, False


def clamp_point_to_bounds(point, text, size, dpi, deg_per_px_x, deg_per_px_y, minx, miny, maxx, maxy):
    """Nudges a label anchor point so its rendered box stays within
    [minx,maxx]x[miny,maxy] — a last-resort safety net for callers that
    can't guarantee the chosen spot actually fits."""
    w = size * 0.62 * (dpi / 72.0) * len(text) * deg_per_px_x
    h = size * 1.3 * (dpi / 72.0) * deg_per_px_y
    x = min(max(point.x, minx + w / 2), maxx - w / 2) if maxx - minx >= w else (minx + maxx) / 2
    y = min(max(point.y, miny + h / 2), maxy - h / 2) if maxy - miny >= h else (miny + maxy) / 2
    return Point(x, y)


def avoid_boxes_for(points, dpi, deg_per_px_x, deg_per_px_y, pad_pt=95):
    """Small exclusion zone around each (lon, lat) clutter point (pins,
    cities) — generous enough to roughly cover their icon plus a nearby
    name label — used to keep the big country-name label from landing
    on top of them."""
    pad_x = pad_pt * dpi / 72.0 * deg_per_px_x
    pad_y = pad_pt * dpi / 72.0 * deg_per_px_y
    return [(x - pad_x, y - pad_y, x + pad_x, y + pad_y) for x, y in points]


def draw_country_name_label(ax, country_geom, country_row, columns, dpi,
                             deg_per_px_x, deg_per_px_y, placed_boxes, avoid_points):
    """Writes the featured country's own name in the biggest open patch
    of its landmass, big and bold, so the map is self-labeled even
    without the title above it. Reserves its footprint so other labels
    (beach pins, cities) get placed clear of it."""
    name_col = next((c for c in ["NAME", "NAME_LONG", "ADMIN", "SOVEREIGNT"] if c in columns), None)
    if name_col is None:
        return
    name = country_row[name_col]
    avoid_boxes = avoid_boxes_for(avoid_points, dpi, deg_per_px_x, deg_per_px_y)
    point, fontsize, _fit_ok = find_open_space_point(country_geom, name, 46, dpi,
                                                       deg_per_px_x, deg_per_px_y, avoid_boxes)
    placed_boxes.append(label_footprint(point.x, point.y, name, fontsize, dpi,
                                         deg_per_px_x, deg_per_px_y, ha="center"))
    ax.text(point.x, point.y, name, color=hex_of("navy_header"), fontsize=fontsize,
             fontweight="bold", ha="center", va="center", zorder=6, clip_on=True)


def draw_city_labels(ax, cities, wrapped, dpi, deg_per_px_x, deg_per_px_y, placed_boxes):
    for city in cities:
        if not city["name"]:
            continue
        lon = city["lon"] + 360 if (wrapped and city["lon"] < 0) else city["lon"]
        marker = "*" if city["is_capital"] else "o"
        size = 24 if city["is_capital"] else 12
        ax.plot(lon, city["lat"], marker, markersize=size,
                 color=hex_of("navy_header"), markeredgecolor="white",
                 markeredgewidth=1.2, zorder=7)
        fontsize = 18
        label_text = f"  {city['name']}"
        placed_boxes.append(label_footprint(lon, city["lat"], label_text, fontsize, dpi,
                                             deg_per_px_x, deg_per_px_y, ha="left"))
        ax.text(lon, city["lat"], label_text, color=hex_of("navy_header"),
                 fontsize=fontsize, fontweight="bold" if city["is_capital"] else "normal",
                 ha="left", va="center", zorder=8, clip_on=True)




_DIVER_FLAG_CACHE = {}


def make_diver_flag_icon(number, px=120):
    """Renders a classic 'diver down' flag (red field, white diagonal
    stripe) on a short pole, with the pin number on a badge at its base,
    as the beach-pin marker. Cached per number since the same number
    always looks identical."""
    if number in _DIVER_FLAG_CACHE:
        return _DIVER_FLAG_CACHE[number]

    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pole_x = px * 0.20
    pole_bottom = px * 0.94
    d.line([(pole_x, px * 0.04), (pole_x, pole_bottom)], fill=(50, 50, 50), width=max(2, px // 30))

    flag_left, flag_top = pole_x, px * 0.04
    flag_right, flag_bottom = px * 0.95, px * 0.50
    d.rectangle([flag_left, flag_top, flag_right, flag_bottom], fill=(206, 26, 26))
    d.line([(flag_left, flag_top), (flag_right, flag_bottom)],
           fill=(255, 255, 255), width=max(3, int((flag_bottom - flag_top) * 0.34)))
    d.rectangle([flag_left, flag_top, flag_right, flag_bottom], outline=(40, 40, 40), width=max(1, px // 60))

    badge_r = px * 0.17
    badge_cx, badge_cy = pole_x, pole_bottom - badge_r * 0.9
    d.ellipse([badge_cx - badge_r, badge_cy - badge_r, badge_cx + badge_r, badge_cy + badge_r],
              fill=(206, 26, 26), outline=(255, 255, 255), width=max(2, px // 40))
    font = load_font(int(badge_r * 1.3), bold=True)
    text = str(number)
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text((badge_cx - tw / 2 - bbox[0], badge_cy - th / 2 - bbox[1]), text, font=font, fill="white")

    _DIVER_FLAG_CACHE[number] = img
    return img


def draw_diver_flag(ax, lon, lat, number):
    icon = make_diver_flag_icon(number)
    imagebox = OffsetImage(np.asarray(icon), zoom=0.45)
    ab = AnnotationBbox(imagebox, (lon, lat), frameon=False, box_alignment=(0.20, 0.06),
                         pad=0, zorder=9)
    ax.add_artist(ab)


def draw_ocean_depth_shading(ax, land_union, minx, miny, maxx, maxy, grid_n=90):
    """Soft depth gradient for the ocean — lighter near coastlines,
    deeper brand blue further out — so a big stretch of open water
    reads as intentional depth shading (like a printed travel map)
    instead of one flat, boring block of color. Land polygons are
    drawn on top afterward, so accuracy right at the coastline doesn't
    matter — this only has to look right offshore."""
    xs = np.linspace(minx, maxx, grid_n)
    ys = np.linspace(miny, maxy, grid_n)
    max_dist = ((maxx - minx) ** 2 + (maxy - miny) ** 2) ** 0.5 * 0.35
    grid = np.empty((len(ys), len(xs)))
    for j, y in enumerate(ys):
        for i, x in enumerate(xs):
            grid[j, i] = min(land_union.distance(Point(x, y)) / max_dist, 1.0)
    cmap = LinearSegmentedColormap.from_list("depth", [hex_of("ocean_light"), hex_of("ocean_blue")])
    # Extent padded slightly past the actual view bounds — imshow's pixel
    # grid can leave a hairline gap at the exact edge otherwise, letting
    # the axes' own (much paler) fallback facecolor show through as a
    # thin light-blue seam down the left/right sides.
    pad_x, pad_y = (maxx - minx) * 0.01, (maxy - miny) * 0.01
    ax.imshow(grid, extent=(minx - pad_x, maxx + pad_x, miny - pad_y, maxy + pad_y), origin="lower", cmap=cmap,
              zorder=0, aspect="auto", interpolation="bilinear")


def geometry_to_mpl_path(geometry):
    """Converts a shapely (Multi)Polygon to a matplotlib Path (honoring
    holes), so it can be used to clip an imshow gradient to a country's
    exact shape."""
    verts, codes = [], []
    polys = geometry.geoms if geometry.geom_type == "MultiPolygon" else [geometry]
    for poly in polys:
        for ring in [poly.exterior, *poly.interiors]:
            coords = list(ring.coords)
            if len(coords) < 3:
                continue
            verts.extend(coords)
            codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(coords) - 2) + [MplPath.CLOSEPOLY])
    return MplPath(verts, codes)


def lighten(rgb_tuple, frac):
    return tuple(min(255, int(c + (255 - c) * frac)) for c in rgb_tuple)


def darken(rgb_tuple, frac):
    return tuple(int(c * (1 - frac)) for c in rgb_tuple)


def draw_country_gradient_fill(ax, geometry, base_hex, zorder=1):
    """Fills a country with a subtle light-to-dark gradient (lighter
    toward the top) instead of one flat color, clipped exactly to its
    shape — a bit of polish/depth per country instead of a flat block,
    without changing the actual hue used to distinguish it."""
    base_rgb = tuple(int(base_hex.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    # LinearSegmentedColormap wants 0-1 floats, not 0-255 ints — every
    # channel was clamping to 1.0 (white) without this conversion.
    # Kept subtle on purpose: label text sits directly on this fill with
    # no outline, so contrast needs to stay roughly consistent wherever
    # a label happens to land, not swing from light to dark across it.
    to_unit = lambda rgb: tuple(c / 255 for c in rgb)
    cmap = LinearSegmentedColormap.from_list(
        "shade", [to_unit(darken(base_rgb, 0.12)), to_unit(lighten(base_rgb, 0.14))])
    grad = np.linspace(0, 1, 256).reshape(256, 1)
    gminx, gminy, gmaxx, gmaxy = geometry.bounds
    im = ax.imshow(grad, extent=(gminx, gmaxx, gminy, gmaxy), origin="lower",
                    cmap=cmap, aspect="auto", zorder=zorder, interpolation="bilinear")
    im.set_clip_path(PathPatch(geometry_to_mpl_path(geometry), transform=ax.transData))


def draw_main_map(world, country_row, pins, cities, marine, out_path, target_w_px, target_h_px):
    dpi = 150
    # Fallback facecolor is the *dark* end of the depth-shading gradient,
    # not the light end — any hairline gap between the gradient imshow
    # and the true axes edge shows this color, and it needs to blend
    # with the (mostly darker, open-water) gradient rather than stand
    # out as a pale seam down the frame's edges.
    ocean = hex_of("ocean_blue")
    fig, ax = plt.subplots(figsize=(target_w_px / dpi, target_h_px / dpi), dpi=dpi, facecolor=ocean)
    ax.set_facecolor(ocean)
    ax.set_aspect("equal")  # preserves true shape — no stretching

    country_geom, wrapped = fix_dateline_wrap(country_row.geometry)
    minx, miny, maxx, maxy = country_geom.bounds
    target_aspect = target_w_px / target_h_px
    minx, miny, maxx, maxy = compute_padded_extent(minx, miny, maxx, maxy, target_aspect)
    view_box = shp_box(minx, miny, maxx, maxy)

    world_to_plot = world
    if wrapped:
        world_to_plot = world.copy()
        world_to_plot["geometry"] = world_to_plot.geometry.apply(
            lambda g: fix_dateline_wrap(g)[0] if g is not None else g
        )

    land_pieces = [country_geom]
    visible_rows = []
    for _, row in world_to_plot.iterrows():
        if row.geometry is None:
            continue
        clipped = row.geometry.intersection(view_box)
        if not clipped.is_empty:
            land_pieces.append(clipped)
            visible_rows.append(row)
    draw_ocean_depth_shading(ax, unary_union(land_pieces), minx, miny, maxx, maxy)

    # Each country gets its own light-to-dark gradient fill (clipped to
    # its real, unclipped shape — clipping to the view box first would
    # draw a fake border line at the edge of the frame) instead of one
    # flat color, for a bit more visual polish.
    name_col = next((c for c in ["NAME", "ADMIN", "SOVEREIGNT"] if c in world_to_plot.columns), None)
    for row in visible_rows:
        color = neighbor_color_for(row[name_col]) if name_col else hex_of("neighbor_land")
        draw_country_gradient_fill(ax, row.geometry, color, zorder=1)
    if visible_rows:
        gpd.GeoSeries([row.geometry for row in visible_rows], crs=world_to_plot.crs).plot(
            ax=ax, facecolor="none", edgecolor="white", linewidth=0.7, zorder=1.5)

    draw_country_gradient_fill(ax, country_geom, hex_of("highlight_land"), zorder=2)
    gpd.GeoSeries([country_geom], crs=world_to_plot.crs).plot(
        ax=ax, facecolor="none", edgecolor="#2f5c3d", linewidth=1.8, zorder=2.5
    )

    draw_neighbor_labels(ax, world_to_plot, country_row.name, view_box)

    deg_per_px_x = (maxx - minx) / target_w_px
    deg_per_px_y = (maxy - miny) / target_h_px
    placed_label_boxes = []

    avoid_points = []
    for city in cities:
        avoid_points.append((city["lon"] + 360 if (wrapped and city["lon"] < 0) else city["lon"], city["lat"]))
    for pin in pins:
        avoid_points.append((pin["lon"] + 360 if (wrapped and pin["lon"] < 0) else pin["lon"], pin["lat"]))

    # Reserve space around the beach pins *before* placing the ocean/sea
    # labels, so a big label like "North Pacific Ocean" actively steers
    # clear of the pin cluster instead of just landing wherever the
    # water body's biggest visible area happens to be.
    placed_label_boxes.extend(avoid_boxes_for(avoid_points, dpi, deg_per_px_x, deg_per_px_y, pad_pt=35))

    marine_to_check = marine
    if wrapped:
        marine_to_check = marine.copy()
        marine_to_check["geometry"] = marine_to_check.geometry.apply(
            lambda g: fix_dateline_wrap(g)[0] if g is not None else g
        )
    draw_ocean_labels(ax, marine_to_check, view_box, placed_label_boxes, dpi, deg_per_px_x, deg_per_px_y)

    draw_country_name_label(ax, country_geom, country_row, world_to_plot.columns,
                             dpi, deg_per_px_x, deg_per_px_y, placed_label_boxes, avoid_points)
    draw_city_labels(ax, cities, wrapped, dpi, deg_per_px_x, deg_per_px_y, placed_label_boxes)

    pt_to_data_x = dpi / 72.0 * deg_per_px_x
    pt_to_data_y = dpi / 72.0 * deg_per_px_y
    # Beach names live in the legend only (see compose_poster), not on
    # the map itself — 5+ names crammed along a coastline never had
    # enough room to read well, however the labels dodged each other.
    # The map keeps just the numbered flags; the number ties back to
    # the (now much bigger) legend entry.
    for pin in pins:
        lon = pin["lon"] + 360 if (wrapped and pin["lon"] < 0) else pin["lon"]
        draw_diver_flag(ax, lon, pin["lat"], pin["number"])
        # Reserve the flag icon's rough footprint so ocean/water labels
        # don't land on top of it.
        placed_label_boxes.append((lon - 8 * pt_to_data_x, pin["lat"] - 5 * pt_to_data_y,
                                    lon + 60 * pt_to_data_x, pin["lat"] + 68 * pt_to_data_y))

    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    fig.savefig(out_path, dpi=dpi, facecolor=ocean)
    plt.close(fig)


def draw_hemisphere_locator(world, country_row, out_path, box_w, box_h):
    """One combined locator: whole-world view with the equator marked
    and the country itself highlighted — answers 'which half of the
    globe' and 'exactly where' in a single image. Both hemispheres are
    drawn identically (no shading) so the equator line is the only cue.
    box_w/box_h must match the view's real aspect ratio (360 wide x
    (LAT_MAX-LAT_MIN) tall) or the map letterboxes instead of filling
    the frame."""
    dpi = 150
    ocean = hex_of("ocean_light")
    fig, ax = plt.subplots(figsize=(box_w / dpi, box_h / dpi), dpi=dpi, facecolor=ocean)
    ax.set_facecolor(ocean)
    ax.set_aspect("equal")

    name_col = next((c for c in ["NAME", "ADMIN", "SOVEREIGNT"] if c in world.columns), None)
    locator_colors = world[name_col].map(neighbor_color_for) if name_col else hex_of("neighbor_land")
    world.plot(ax=ax, color=locator_colors, edgecolor="white", linewidth=0.25)

    # Highlight the country: fill its true shape, and also drop a bold
    # dot on its centroid so small countries (Fiji, Costa Rica, etc.)
    # are still clearly visible at whole-world scale.
    gpd.GeoSeries([country_row.geometry]).plot(ax=ax, color=hex_of("brick_red"), zorder=4)
    center = country_row.geometry.representative_point()
    ax.plot(center.x, center.y, "o", markersize=10, color=hex_of("brick_red"),
             markeredgecolor="black", markeredgewidth=1.8, zorder=5)

    ax.axhline(0, color="#555555", linewidth=0.5, linestyle="--", zorder=3)  # equator line
    ax.set_xlim(-180, 180)
    ax.set_ylim(LOCATOR_LAT_MIN, LOCATOR_LAT_MAX)
    ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    fig.savefig(out_path, dpi=dpi, facecolor=ocean)
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


def make_vertical_gradient(w, h, color_a, color_b):
    """Simple top-to-bottom linear gradient — same idea as the ocean's
    depth shading, applied to a big flat-colored panel so it reads as
    intentional shading instead of one flat block."""
    row = np.linspace(0, 1, h).reshape(h, 1, 1)
    arr = np.array(color_a) + (np.array(color_b) - np.array(color_a)) * row
    return Image.fromarray(arr.astype(np.uint8), "RGB").resize((w, h))


def compose_poster(country_name, facts, pins, main_map_path, locator_path, out_path):
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), rgb("white"))
    draw = ImageDraw.Draw(canvas)

    # ---- Header bar ---- both contact pieces (website + email) go up
    # top since that's the whole point of the brand; "map series" is
    # secondary and moved to the footer instead (see below). This
    # poster is viewed on phone screens, scaled way down — these corner
    # texts need to survive that, so they're sized to fill most of the
    # bar height, not just "readable at full size." All four corners of
    # the poster (this bar + the footer) use plain white — highest
    # possible contrast against navy, and neutral enough not to
    # visually clash with the warm sand strip just below this bar.
    draw.rectangle([0, 0, CANVAS_W, HEADER_H], fill=rgb("navy_header"))
    f_header = load_font(56, bold=True)
    f_header_email = load_font(48, bold=True)
    draw.text((30, (HEADER_H - 56) / 2 - 6), WEBSITE.upper(), font=f_header, fill=rgb("white"))
    w = draw.textlength(CONTACT_EMAIL, font=f_header_email)
    draw.text((CANVAS_W - w - 30, (HEADER_H - 48) / 2 - 4), CONTACT_EMAIL, font=f_header_email, fill=rgb("white"))

    # ---- Top strip: flag, name, facts (left) + locator (right) ----
    strip_y0 = HEADER_H
    strip_y1 = HEADER_H + TOP_STRIP_H
    strip_gradient = make_vertical_gradient(CANVAS_W, TOP_STRIP_H, rgb("sand"), rgb("sand_deep"))
    canvas.paste(strip_gradient, (0, strip_y0))
    # Frame it so the sand panel reads as its own zone instead of
    # blending into the map's similarly pale land color right below it.
    # Drawn now, before any strip content, so labels that slightly
    # overhang the frame (e.g. "WHERE IN THE WORLD" above the locator)
    # still render on top of it instead of getting cut by it.
    draw.rectangle([0, strip_y0, CANVAS_W - 1, strip_y1], outline=rgb("navy_header"), width=5)

    # Country name column starts at a fixed x — computed first so the
    # flag (below) can be centered against where this text *actually*
    # renders, not a guessed column width. The title is itself centered
    # within this column, so its real left edge is well right of name_x.
    name_x = 520
    name_max_w = CANVAS_W - LOCATOR_W - name_x - 40
    f_title = autosize_font(draw, country_name.upper(), name_max_w, start_size=72, min_size=32)
    title_w = draw.textlength(country_name.upper(), font=f_title)
    title_x = name_x + (name_max_w - title_w) / 2

    # Flag — fit, never stretched, uniform frame. Centered between the
    # left edge of the poster and wherever the title text actually
    # starts (not a fixed guess), and sized generously since most flags
    # share a similar aspect ratio.
    try:
        flag_raw = download_flag(facts["cca2"])
        flag_fitted = fit_image_in_box(flag_raw, FLAG_BOX_W, FLAG_BOX_H)
    except Exception as e:
        print(f"  Warning: could not load flag ({e}) — using placeholder.")
        flag_fitted = placeholder_flag(FLAG_BOX_W, FLAG_BOX_H)
    flag_x = max(20, int((title_x - FLAG_BOX_W) / 2))
    flag_y = int(strip_y0 + (TOP_STRIP_H - FLAG_BOX_H) / 2)
    draw.rectangle([flag_x - 4, flag_y - 4, flag_x + FLAG_BOX_W + 4, flag_y + FLAG_BOX_H + 4],
                    outline=rgb("ocean_blue"), width=3)
    canvas.paste(flag_fitted, (flag_x, flag_y), flag_fitted)

    draw.text((title_x, strip_y0 + 40), country_name.upper(), font=f_title, fill=rgb("text_dark"))

    # Brand artwork — fixed box, same fit/center treatment as the flag,
    # placed under the title in place of the old facts block. Starts
    # right below the title's max possible height and runs down to
    # near the strip's bottom edge — as big as this space allows.
    if os.path.exists(ARTWORK_PATH):
        artwork_fitted = fit_image_in_box(Image.open(ARTWORK_PATH).convert("RGBA"), ARTWORK_BOX_W, ARTWORK_BOX_H)
        artwork_x = int(name_x + (name_max_w - ARTWORK_BOX_W) / 2)
        artwork_y = strip_y0 + 135
        canvas.paste(artwork_fitted, (artwork_x, artwork_y), artwork_fitted)

    # Locator — top right corner of the strip. Sized to its true aspect
    # ratio (see LOCATOR_H) so it fills the box with no letterboxing,
    # and stays well clear of the main map paste below it.
    loc_x = CANVAS_W - LOCATOR_W - 20
    loc_y = strip_y0 + 20
    loc_img = Image.open(locator_path).resize((LOCATOR_W, LOCATOR_H))
    canvas.paste(loc_img, (loc_x, loc_y))
    draw.rectangle([loc_x, loc_y, loc_x + LOCATOR_W, loc_y + LOCATOR_H],
                    outline=rgb("ocean_blue"), width=3)
    draw.text((loc_x, loc_y - 26), "WHERE IN THE WORLD", font=load_font(18, bold=True), fill=rgb("ocean_blue"))

    # ---- Main map (fills remaining space below the strip) ----
    map_y0 = strip_y1
    map_y1 = CANVAS_H - FOOTER_H
    main_img = Image.open(main_map_path)
    canvas.paste(main_img, (0, map_y0))

    # A dedicated divider, drawn *after* the map paste (the top strip's
    # own border line sits exactly at this y and was getting overwritten
    # by the paste above, effectively erasing it).
    draw.rectangle([0, map_y0 - 3, CANVAS_W, map_y0 + 3], fill=rgb("text_dark"))

    # Beach pin key, bottom-left overlay on the map — wraps into another
    # column instead of running off the map when there are many pins.
    # Sized big — this is the legend for the entire point of the map
    # (which beach is which number), so it can't be the part that's
    # hard to read.
    key_x0, key_y0 = 30, map_y0 + 20
    key_col_w = 440
    key_row_h = 52
    key_y_max = map_y1 - 20
    key_x, key_y = key_x0, key_y0
    f_pin_num = load_font(24, bold=True)
    f_pin_name = load_font(28, bold=True)
    for pin in pins:
        if key_y + key_row_h > key_y_max:
            key_x += key_col_w
            key_y = key_y0
        cx, cy, r = key_x + 20, key_y + 20, 21
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=rgb("brick_red"), outline=rgb("white"), width=3)
        num_w = draw.textlength(str(pin["number"]), font=f_pin_num)
        draw.text((cx - num_w / 2, cy - 15), str(pin["number"]), font=f_pin_num, fill=rgb("white"))
        draw.text((key_x + 50, key_y + 6), pin["name"], font=f_pin_name, fill=rgb("text_dark"))
        key_y += key_row_h

    # ---- Footer bar ---- tagline stays put; "map series" moved down
    # here from the header (contact info took over the top bar) — same
    # big/bold/white treatment either way.
    draw.rectangle([0, CANVAS_H - FOOTER_H, CANVAS_W, CANVAS_H], fill=rgb("navy_header"))
    footer_half_w = CANVAS_W / 2 - 50
    f_footer_tagline = autosize_font(draw, TAGLINE, footer_half_w, start_size=40, bold=True, min_size=22)
    map_series_label = "BEACH BUM BLUEPRINT MAP SERIES"
    f_footer_series = autosize_font(draw, map_series_label, footer_half_w, start_size=38, bold=True, min_size=22)
    tagline_h = f_footer_tagline.getbbox(TAGLINE)[3]
    draw.text((30, (CANVAS_H - FOOTER_H) + (FOOTER_H - tagline_h) / 2), TAGLINE,
              font=f_footer_tagline, fill=rgb("white"))
    w = draw.textlength(map_series_label, font=f_footer_series)
    series_h = f_footer_series.getbbox(map_series_label)[3]
    draw.text((CANVAS_W - w - 30, (CANVAS_H - FOOTER_H) + (FOOTER_H - series_h) / 2), map_series_label,
              font=f_footer_series, fill=rgb("white"))

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

    print(f"1/7  Looking up facts for {country_name}...")
    facts = get_country_facts(country_name)

    print("2/7  Loading world boundary data...")
    world = load_world_boundaries()
    country_row = get_country_geometry(world, country_name)

    print("3/7  Geocoding featured beach pins...")
    pins = load_beach_pins(args.pins, country_name)
    if not pins:
        print(f"  No pins found for '{country_name}' in {args.pins} — "
              f"add rows there first (see beach_pins_template.csv).")

    print("4/7  Finding cities to label...")
    cities_gdf = load_world_cities()
    cities = get_country_cities(cities_gdf, country_row, country_name)

    print("5/7  Loading ocean/sea names...")
    marine = load_world_marine()

    print("6/7  Drawing maps...")
    main_map_path = os.path.join(tmp_dir, "main_map.png")
    locator_path = os.path.join(tmp_dir, "locator.png")
    main_map_w = CANVAS_W
    main_map_h = CANVAS_H - HEADER_H - TOP_STRIP_H - FOOTER_H
    draw_main_map(world, country_row, pins, cities, marine, main_map_path, main_map_w, main_map_h)
    draw_hemisphere_locator(world, country_row, locator_path, LOCATOR_W, LOCATOR_H)

    print("7/7  Composing final poster...")
    compose_poster(facts["name"], facts, pins, main_map_path, locator_path, out_path)

    print(f"Done: {out_path}")


if __name__ == "__main__":
    main()
