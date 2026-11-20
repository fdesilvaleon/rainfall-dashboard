"""Builds the boundary files for the trail-region map. Run by hand, not from GitHub Actions:

    pip install shapely pyproj pyshp
    python tools/build_boundaries.py                 # parks and cities
    python tools/build_boundaries.py --only cities   # just one of them

It reads data/regions.json (which park or city is in which region) and writes:
  data/parks.geojson   one feature per park (properties: name, region, source)
  data/cities.geojson  one feature per city (same properties)
and prints a report: what was matched, its area, whether it has several pieces,
the nearest rain gauge, and parks that overlap a city or another region's park.

Sources (see README, "Boundary data", for licenses and attribution):
  parks:  OpenStreetMap, through the Overpass API (one request for all parks)
  cities: US Census Bureau cartographic boundary file for California places

Shapes are simplified (SIMPLIFY_M metres) and coordinates rounded to 5 decimals.
--cache-dir DIR saves the downloads in DIR and reuses them on later runs.
"""
import argparse
import csv
import io
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import shapefile  # pyshp
from pyproj import Transformer
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping, shape
import shapely
from shapely.ops import linemerge, polygonize, polygonize_full, unary_union

ROOT = Path(__file__).resolve().parent.parent
REGIONS = ROOT / "data" / "regions.json"
PARKS_OUT = ROOT / "data" / "parks.geojson"
CITIES_OUT = ROOT / "data" / "cities.geojson"
GAUGES = ROOT / "gauges.csv"

USER_AGENT = "rainfall-dashboard boundary build (github.com/fdesilvaleon/rainfall-dashboard)"
OVERPASS = "https://overpass-api.de/api/interpreter"
# Other public Overpass servers (same OpenStreetMap data), tried in turn if the main one can't be reached.
OVERPASS_FALLBACKS = ["https://overpass.kumi.systems/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
CENSUS_YEAR = 2025
CENSUS_ZIP = f"https://www2.census.gov/geo/tiger/GENZ{CENSUS_YEAR}/shp/cb_{CENSUS_YEAR}_06_place_500k.zip"
AREA = (37.05, -122.45, 37.55, -121.60)  # south, west, north, east: where to look for parks
I280_AREA = (37.15, -122.35, 37.55, -121.85)  # where to fetch Interstate 280
MIN_PIECE_KM2 = 0.05                          # trimmed city pieces smaller than this (5 ha) are dropped as slivers
SIMPLIFY_M = 20                          # simplification tolerance in metres
DECIMALS = 5                             # about 1 m

TO_M = Transformer.from_crs("EPSG:4326", "EPSG:32610", always_xy=True).transform    # lon/lat -> UTM 10N metres
TO_DEG = Transformer.from_crs("EPSG:32610", "EPSG:4326", always_xy=True).transform


def transform(func, geom):
    return shapely.transform(geom, lambda x, y: func(x, y), interleaved=False)


# ---------- downloads ----------

def overpass(query, cache, name):
    """Run an Overpass query, trying the fallback servers if the main one fails."""
    error = None
    for url in [OVERPASS] + OVERPASS_FALLBACKS:
        try:
            return json.loads(download(url, cache, name, data={"data": query}))
        except Exception as e:
            error = e
            print(f"{name}: {url} failed ({e})", file=sys.stderr)
    raise RuntimeError(f"no Overpass server answered ({error})")


def download(url, cache, name, data=None, timeout=300):
    """One request; if it fails, one more try after a minute (Overpass is sometimes busy)."""
    path = Path(cache) / name if cache else None
    if path and path.exists():
        return path.read_bytes()
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"{name}: {e}; trying again in 60 seconds", file=sys.stderr)
        time.sleep(60)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return raw


# ---------- geometry helpers ----------

def clean(geom, tolerance=SIMPLIFY_M):
    """Simplify in metres, round to DECIMALS, and return a valid (Multi)Polygon in lon/lat."""
    g = transform(TO_M, geom).buffer(0).simplify(tolerance, preserve_topology=True)
    g = transform(TO_DEG, g)
    rnd = lambda ring: [(round(x, DECIMALS), round(y, DECIMALS)) for x, y in ring]
    polys = []
    for p in getattr(g, "geoms", [g]):
        if p.is_empty or p.geom_type != "Polygon":
            continue
        q = Polygon(rnd(p.exterior.coords), [rnd(i.coords) for i in p.interiors]).buffer(0)
        polys.extend(getattr(q, "geoms", [q]))
    polys = [p for p in polys if not p.is_empty and p.area > 0]
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def rounded_mapping(geom):
    """GeoJSON geometry with coordinates rounded (buffer(0) can add digits)."""
    def r(x):
        if isinstance(x, (list, tuple)):
            if x and isinstance(x[0], float):
                return [round(x[0], DECIMALS), round(x[1], DECIMALS)]
            return [r(y) for y in x]
        return x
    m = mapping(geom)
    return {"type": m["type"], "coordinates": r(m["coordinates"])}


def area_km2(geom):
    return transform(TO_M, geom).area / 1e6


def pieces(geom):
    return len(getattr(geom, "geoms", [geom]))


def element_geometry(el):
    """Shapely geometry of an Overpass element fetched with 'out geom' (closed way or multipolygon relation)."""
    if el["type"] == "way":
        pts = [(p["lon"], p["lat"]) for p in el.get("geometry", [])]
        return Polygon(pts).buffer(0) if len(pts) >= 4 and pts[0] == pts[-1] else None
    outer, inner = [], []
    for m in el.get("members", []):
        if m.get("type") != "way" or not m.get("geometry"):
            continue
        line = LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
        (inner if m.get("role") == "inner" else outer).append(line)
    if not outer:
        return None
    # Outline pieces that don't join into closed rings are left out; count them so the report can warn.
    _, dangles, cuts, invalid = polygonize_full(linemerge(outer))
    loose = sum(len(getattr(g, "geoms", [])) for g in (dangles, cuts, invalid))
    if loose:
        BROKEN.append(f"{el['type']} {el['id']} ({el.get('tags', {}).get('name')}): {loose} outline piece(s) don't close into a ring and were left out")
    shell = unary_union(list(polygonize(linemerge(outer))))
    holes = unary_union(list(polygonize(linemerge(inner)))) if inner else None
    geom = shell.difference(holes) if holes is not None and not holes.is_empty else shell
    return None if geom.is_empty else geom.buffer(0)


# ---------- parks (OpenStreetMap) ----------

BROKEN = []  # warnings from element_geometry
PARK_TAGS = {("leisure", "park"), ("leisure", "nature_reserve"), ("boundary", "protected_area"), ("boundary", "national_park")}


def fetch_parks(parks, cache):
    words = sorted({m for p in parks for m in p["match"]}, key=len)
    pattern = "|".join(re.escape(w) for w in words)
    s, w, n, e = AREA
    pinned = [x for p in parks for x in p.get("also", []) + ([p["osm"]] if p.get("osm") else []) if re.fullmatch(r"(relation|way)/\d+", x)]
    extra = "".join(f"{x.split('/')[0]}({x.split('/')[1]});" for x in pinned)
    # (Names under "also" contain a match name, so the name search below already finds them.)
    query = (f'[out:json][timeout:180];(relation["name"~"{pattern}",i]({s},{w},{n},{e});'
             f'way["name"~"{pattern}",i]({s},{w},{n},{e});{extra});out geom;')
    return overpass(query, cache, "overpass_parks.json")["elements"]


def match_park(park, elements):
    """The OSM elements for one park: a pinned id, else every park-like element whose name equals the
    first match name that finds anything (pieces mapped separately are joined). Returns (elements, matched name)."""
    parklike = [el for el in elements if any(el.get("tags", {}).get(k) == v for k, v in PARK_TAGS)]
    if park.get("osm"):
        kind, num = park["osm"].split("/")
        found = [el for el in elements if el["type"] == kind and str(el["id"]) == num]
        return found, found[0]["tags"].get("name") if found else None
    for name in park["match"]:
        found = [el for el in parklike if el.get("tags", {}).get("name", "").lower() == name.lower()]
        if found:
            return found + also_elements(park, elements, found), found[0]["tags"]["name"]
    return [], None


def also_elements(park, elements, already):
    """Extra pieces listed under "also": exact names, or "relation/123" / "way/123"."""
    out = []
    for item in park.get("also", []):
        if re.fullmatch(r"(relation|way)/\d+", item):
            kind, num = item.split("/")
            hits = [el for el in elements if el["type"] == kind and str(el["id"]) == num]
        else:
            hits = [el for el in elements if el.get("tags", {}).get("name", "").lower() == item.lower()]
        if not hits:
            BROKEN.append(f"{park['name']}: extra piece {item!r} not found")
        out += [el for el in hits if el not in already and el not in out]
    return out


def build_parks(cfg, cache, report):
    elements = fetch_parks(cfg["parks"], cache)
    features = []
    for park in cfg["parks"]:
        found, matched = match_park(park, elements)
        geoms = [g for g in (element_geometry(el) for el in found) if g is not None]
        if not geoms:
            report.append({"kind": "park", "name": park["name"], "region": park["region"], "matched": None,
                           "candidates": sorted({el["tags"].get("name") for el in elements
                                                 if any(m.lower() in el.get("tags", {}).get("name", "").lower() for m in park["match"])})})
            continue
        geom = clean(unary_union(geoms))
        ids = ", ".join(f"{el['type']} {el['id']}" for el in found)
        features.append(feature(park, geom, f"OpenStreetMap {ids} ({matched})"))
        report.append({"kind": "park", "name": park["name"], "region": park["region"], "matched": matched, "osm": ids,
                       "tags": sorted({f"{k}={v}" for el in found for k, v in el.get("tags", {}).items() if (k, v) in PARK_TAGS}),
                       "geom": geom})
    return features


# ---------- cities (US Census) ----------

def census_city_limits(cfg, cache):
    """The full (untrimmed) city limits from the Census file: {name: (geometry, Census record, file name)}."""
    z = zipfile.ZipFile(io.BytesIO(download(CENSUS_ZIP, cache, Path(CENSUS_ZIP).name)))
    base = next(n for n in z.namelist() if n.endswith(".shp"))[:-4]
    reader = shapefile.Reader(shp=io.BytesIO(z.read(base + ".shp")), dbf=io.BytesIO(z.read(base + ".dbf")),
                              shx=io.BytesIO(z.read(base + ".shx")))
    by_id = {rec["GEOID"]: (rec, shp) for rec, shp in zip(reader.records(), reader.shapes())}
    out = {}
    for city in cfg["cities"]:
        rec, shp = by_id.get(city["census_geoid"], (None, None))
        if rec is not None:
            out[city["name"]] = (shape(shp.__geo_interface__).buffer(0), rec, base)
    return out


def east_of_280(cache):
    """The area east of Interstate 280 (the bay side), from the motorway's two carriageways in OpenStreetMap.
    Each carriageway is closed into an area with lines out to the east; a place must be east of both."""
    s, w, n, e = I280_AREA
    q = f'[out:json][timeout:120];way["highway"="motorway"]["ref"~"(^|;)I 280($|;)"]({s},{w},{n},{e});out geom;'
    els = overpass(q, cache, "overpass_i280.json")["elements"]
    lines = [LineString([(p["lon"], p["lat"]) for p in el["geometry"]]) for el in els if len(el.get("geometry", [])) > 1]
    merged = linemerge(lines)
    parts = sorted(getattr(merged, "geoms", [merged]), key=lambda g: g.length, reverse=True)[:2]
    areas = []
    for line in parts:
        pts = list(line.coords)
        if pts[0][1] < pts[-1][1]:
            pts.reverse()  # start at the north end
        (x0, y0), (x1, y1) = pts[0], pts[-1]
        far_east, far_north = -121.0, 38.0
        areas.append(Polygon(pts + [(far_east, y1), (far_east, far_north), (x0, far_north)]).buffer(0))
    east = areas[0].intersection(areas[1]) if len(areas) == 2 else areas[0]
    lats = [y for line in parts for _, y in line.coords]
    return east, f"{len(els)} I-280 motorway ways, {len(parts)} carriageways, latitude {min(lats):.3f} to {max(lats):.3f}"


def build_cities(cfg, cache, report, park_geoms=None, trim=True):
    """City limits. With trim: only the part east of I-280, minus every park, and without slivers under MIN_PIECE_KM2."""
    limits = census_city_limits(cfg, cache)
    east = None
    if trim:
        if not park_geoms:
            raise RuntimeError("the parks are needed to trim the cities, and they weren't built")
        east, info = east_of_280(cache)
        report.append({"kind": "note", "text": info})
        parks_all = unary_union(list(park_geoms.values()))
    features = []
    for city in cfg["cities"]:
        if city["name"] not in limits:
            report.append({"kind": "city", "name": city["name"], "region": city["region"], "matched": None})
            continue
        full, rec, base = limits[city["name"]]
        source = f"US Census Bureau {CENSUS_YEAR} cartographic boundary file ({base}), GEOID {rec['GEOID']}"
        geom = full
        if trim:
            geom = full.intersection(east).difference(parks_all)
            keep = [p for p in getattr(geom, "geoms", [geom]) if p.geom_type == "Polygon" and area_km2(p) >= MIN_PIECE_KM2]
            geom = unary_union(keep)
            source += "; trimmed to east of I-280 and without parks, using OpenStreetMap data"
        geom = clean(geom)
        features.append(feature(city, geom, source))
        report.append({"kind": "city", "name": city["name"], "region": city["region"], "matched": rec["NAMELSAD"],
                       "geom": geom, "full_km2": area_km2(full)})
    return features


# ---------- output ----------

def feature(item, geom, source):
    return {"type": "Feature", "properties": {"name": item["name"], "region": item["region"], "source": source},
            "geometry": rounded_mapping(geom)}


def write_collection(path, features, note):
    """One feature per line, so diffs stay readable."""
    lines = [json.dumps(f, separators=(",", ":")) for f in features]
    text = ('{"type":"FeatureCollection",\n"note":' + json.dumps(note) + ',\n"features":[\n' + ",\n".join(lines) + "\n]}\n")
    Path(path).write_text(text)
    return len(text.encode())


def distance_km(geom, lat, lon):
    return transform(TO_M, geom).distance(Point(TO_M(lon, lat))) / 1000


def print_report(report, gauges):
    print(f"\n{'name':38} {'region':9} {'matched as':42} {'km2':>7} pieces  nearest gauge")
    for r in report:
        if r.get("kind") == "note":
            continue
        if not r.get("matched"):
            print(f"{r['name']:38} {r['region']:9} NOT MATCHED" + (f"  (similar names: {r['candidates']})" if r.get("candidates") else ""))
            continue
        g = r["geom"]
        near = min(((distance_km(g, float(x["lat"]), float(x["lon"])), x) for x in gauges), key=lambda t: t[0])
        print(f"{r['name']:38} {r['region']:9} {r['matched'][:42]:42} {area_km2(g):7.2f} {pieces(g):6}  "
              f"{near[1]['id']} {near[1]['name']} ({near[0]:.1f} km{', inside' if near[0] == 0 else ''})"
              + (f"  [full city limits {r['full_km2']:.2f} km2]" if r.get("full_km2") else ""))
    # Overlaps: a park inside a city (valley region), or touching a park of another region.
    for r in report:
        if r.get("kind") == "note":
            print(r["text"])
    done = [r for r in report if r.get("matched")]
    print("\nOverlaps between regions (over 1% of the smaller shape), and parks of different regions within 100 m:")
    any_found = False
    for i, a in enumerate(done):
        for b in done[i + 1:]:
            if a["region"] == b["region"]:
                continue
            ga, gb = transform(TO_M, a["geom"]), transform(TO_M, b["geom"])
            share = ga.intersection(gb).area / min(ga.area, gb.area)
            if share > 0.01 or (a["kind"] == b["kind"] == "park" and ga.distance(gb) < 100):
                any_found = True
                what = f"{share:.0%} overlap" if share > 0.01 else f"{ga.distance(gb):.0f} m apart"
                print(f"  {a['name']} ({a['region']}) / {b['name']} ({b['region']}): {what}")
    if not any_found:
        print("  none")
    print("\nOutline problems:")
    print("\n".join(f"  {b}" for b in BROKEN) if BROKEN else "  none")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", choices=["parks", "cities"])
    parser.add_argument("--cache-dir", help="save downloads here and reuse them")
    parser.add_argument("--untrimmed", action="store_true", help="write the full city limits (not trimmed to east of I-280)")
    args = parser.parse_args()
    cfg = json.loads(REGIONS.read_text())
    ids = {r["id"] for r in cfg["regions"]}
    bad = [x["name"] for x in cfg["parks"] + cfg["cities"] if x["region"] not in ids]
    if bad:
        sys.exit(f"Unknown region for: {', '.join(bad)}")
    with open(GAUGES, newline="") as f:
        gauges = list(csv.DictReader(f))
    report, failed = [], False
    city_note = (f"City limits for the trail-region map: the part east of Interstate 280, without parks. Sources: US Census Bureau "
                 f"{CENSUS_YEAR} cartographic boundary files (public domain); I-280 and park outlines (c) OpenStreetMap contributors, "
                 f"ODbL 1.0 (openstreetmap.org/copyright). Built by tools/build_boundaries.py; simplified to about {SIMPLIFY_M} m.")
    if args.untrimmed:
        city_note = (f"City limits (untrimmed) for the trail-region map. Source: US Census Bureau {CENSUS_YEAR} cartographic boundary "
                     f"files (public domain). Built by tools/build_boundaries.py --untrimmed; simplified to about {SIMPLIFY_M} m.")
    park_note = (f"Park boundaries for the trail-region map. Source: (c) OpenStreetMap contributors, ODbL 1.0 "
                 f"(openstreetmap.org/copyright). Built by tools/build_boundaries.py; simplified to about {SIMPLIFY_M} m.")
    park_geoms = None
    # Parks first: the cities are trimmed with them.
    if args.only != "cities" or not args.untrimmed:
        try:
            features = build_parks(cfg, args.cache_dir, report)
            park_geoms = {r["name"]: r["geom"] for r in report if r.get("kind") == "park" and r.get("geom") is not None}
            if args.only != "cities":
                size = write_collection(PARKS_OUT, features, park_note)
                print(f"Wrote {PARKS_OUT.relative_to(ROOT)}: {len(features)} features, {size / 1024:.0f} KB")
        except Exception as e:
            print(f"parks: FAILED to download or build ({e}); {PARKS_OUT.name} left unchanged", file=sys.stderr)
            failed = True
    if args.only != "parks":
        try:
            features = build_cities(cfg, args.cache_dir, report, park_geoms, trim=not args.untrimmed)
            size = write_collection(CITIES_OUT, features, city_note)
            print(f"Wrote {CITIES_OUT.relative_to(ROOT)}: {len(features)} features, {size / 1024:.0f} KB")
        except Exception as e:
            print(f"cities: FAILED to download or build ({e}); {CITIES_OUT.name} left unchanged", file=sys.stderr)
            failed = True
    print_report(report, gauges)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
