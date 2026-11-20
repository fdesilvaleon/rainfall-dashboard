"""Temporary helper (run on GitHub's machines by the boundary workflow): finds out why some Midpen
preserve outlines are too small, and what official Midpen data exists. Writes a plain-text report.

    python tools/diagnose_boundaries.py > tools/boundaries_diagnosis.txt
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_boundaries as bb  # noqa: E402

FRAGMENTS = ["Rancho San Antonio", "Russian Ridge", "Skyline Ridge", "Monte Bello", "Long Ridge",
             "Windy Hill", "Los Trancos", "Arastradero", "Coal Creek", "Saratoga Gap", "Picchetti"]
MIDPEN_PAGES = {
    "Rancho San Antonio": "rancho-san-antonio", "Russian Ridge": "russian-ridge", "Skyline Ridge": "skyline-ridge",
    "Monte Bello": "monte-bello", "Long Ridge": "long-ridge", "Windy Hill": "windy-hill", "Los Trancos": "los-trancos",
}


def get(url, data=None, timeout=180):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": bb.USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def section(title):
    print(f"\n===== {title} =====")


def osm_candidates():
    section("OpenStreetMap: every object whose name contains a preserve name, or run by Midpen")
    s, w, n, e = bb.AREA
    pattern = "|".join(FRAGMENTS)
    q = (f'[out:json][timeout:180];('
         f'relation["name"~"{pattern}",i]({s},{w},{n},{e});way["name"~"{pattern}",i]({s},{w},{n},{e});'
         f'relation["operator"~"Midpeninsula|Midpen",i]({s},{w},{n},{e});way["operator"~"Midpeninsula|Midpen",i]({s},{w},{n},{e});'
         f');out geom;')
    els = json.loads(get(bb.OVERPASS, {"data": q}))["elements"]
    rows = []
    for el in els:
        t = el.get("tags", {})
        if t.get("highway") or t.get("waterway") or t.get("route"):
            continue  # roads, trails, creeks: not areas
        g = bb.element_geometry(el)
        area = bb.area_km2(g) if g is not None and not g.is_empty else 0
        closed = el["type"] == "relation" or (el.get("geometry") and el["geometry"][0] == el["geometry"][-1])
        keys = {k: v for k, v in t.items() if k in ("leisure", "boundary", "landuse", "protect_class", "operator", "owner", "protection_title", "access", "type")}
        rows.append((t.get("name", ""), el["type"], el["id"], round(area, 2), closed, keys))
    for r in sorted(rows):
        print(f"{r[0][:55]:55} {r[1]:8} {r[2]:>11} {r[3]:7.2f} km2  closed={r[4]}  {r[5]}")
    print("\nRelations named for the target preserves, with their members:")
    for el in els:
        name = el.get("tags", {}).get("name", "")
        if el["type"] == "relation" and any(f.lower() in name.lower() for f in FRAGMENTS[:8]):
            roles = {}
            for m in el.get("members", []):
                roles[(m["type"], m.get("role", ""))] = roles.get((m["type"], m.get("role", "")), 0) + 1
            print(f"  relation {el['id']} {name}: members {roles}")
    bb.BROKEN and print("\nOutline problems:\n  " + "\n  ".join(bb.BROKEN))


def i280():
    section("OpenStreetMap: Interstate 280")
    s, w, n, e = 37.15, -122.35, 37.55, -121.85
    q = f'[out:json][timeout:120];way["highway"="motorway"]["ref"~"(^|;)I 280($|;)"]({s},{w},{n},{e});out geom;'
    els = json.loads(get(bb.OVERPASS, {"data": q}))["elements"]
    lats = [p["lat"] for el in els for p in el.get("geometry", [])]
    print(f"{len(els)} motorway ways, latitudes {min(lats):.3f} to {max(lats):.3f}" if lats else "nothing found")


def arcgis_search():
    section("ArcGIS Online: official Midpen boundary data")
    seen = set()
    for q in ['Midpeninsula Regional Open Space District preserves', 'Midpen preserve boundaries', 'owner:midpen*', 'Midpen open space preserves boundary']:
        try:
            res = json.loads(get("https://www.arcgis.com/sharing/rest/search?" + urllib.parse.urlencode({"q": q, "f": "json", "num": 30})))
        except Exception as e:
            print(f"search {q!r}: FAILED {e}")
            continue
        for it in res.get("results", []):
            if it["id"] in seen or it.get("type") not in ("Feature Service", "Map Service", "GeoJson", "Shapefile", "File Geodatabase", "CSV"):
                continue
            seen.add(it["id"])
            lic = re.sub(r"<[^>]+>", " ", it.get("licenseInfo") or "").strip()
            acc = re.sub(r"<[^>]+>", " ", it.get("accessInformation") or "").strip()
            print(f"- {it.get('title')} | type {it.get('type')} | owner {it.get('owner')} | id {it['id']}\n"
                  f"    url {it.get('url')}\n    license: {lic[:400] or '(none given)'}\n    credits: {acc[:200] or '(none)'}")
        time.sleep(2)


def midpen_acreage():
    section("Midpen website: published acreage")
    for name, slug in MIDPEN_PAGES.items():
        url = f"https://www.openspace.org/preserves/{slug}"
        try:
            text = re.sub(r"<[^>]+>", " ", get(url).decode("utf-8", "replace"))
            hits = sorted(set(re.findall(r"([\d,]{2,7})\s*(?:-|\s)?acres?", text, re.I)))
            print(f"{name}: {', '.join(hits) or 'no acreage found'}  ({url})")
        except Exception as e:
            print(f"{name}: FAILED {e}")
        time.sleep(2)


if __name__ == "__main__":
    for step in (osm_candidates, i280, arcgis_search, midpen_acreage):
        try:
            step()
        except Exception as e:
            print(f"{step.__name__}: FAILED {type(e).__name__}: {e}")
        time.sleep(5)
