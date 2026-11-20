"""Checks the boundary data for the trail-region map (data/regions.json, data/parks.geojson,
data/cities.geojson). Uses only the standard library, so it runs in the hourly workflow too."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
REGIONS = {"foothills": "Peninsula foothills", "skyline": "Skyline ridge", "south": "South Valley", "valley": "Valley floor"}
AREA = (36.9, -122.6, 37.7, -121.4)  # south, west, north, east: generous box around the region
MAX_FILE = 500 * 1024
MAX_TOTAL = 1.5 * 1024 * 1024


def area_km2(geometry):
    """Approximate area (km2) of a GeoJSON Polygon/MultiPolygon: shoelace on a local flat projection."""
    import math
    polys = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    total = 0.0
    for poly in polys:
        for k, ring in enumerate(poly):
            lat0 = math.radians(sum(p[1] for p in ring) / len(ring))
            xy = [(lon * 111.320 * math.cos(lat0), lat * 110.574) for lon, lat in ring]
            a = abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(xy, xy[1:]))) / 2
            total += a if k == 0 else -a  # holes count negative
    return total


def contains(geometry, lon, lat):
    """Point-in-polygon (ray casting), holes included."""
    polys = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    def in_ring(ring):
        inside = False
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        return inside
    return any(in_ring(poly[0]) and not any(in_ring(h) for h in poly[1:]) for poly in polys)


def load(name):
    return json.loads((DATA / name).read_text())


def rings(geometry):
    if geometry["type"] == "Polygon":
        return [r for r in geometry["coordinates"]]
    return [r for poly in geometry["coordinates"] for r in poly]


class RegionsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load("regions.json")

    def test_regions_and_names(self):
        self.assertEqual({r["id"]: r["name"] for r in self.cfg["regions"]}, REGIONS)

    def test_every_park_and_city_has_a_valid_region(self):
        ids = {r["id"] for r in self.cfg["regions"]}
        for item in self.cfg["parks"] + self.cfg["cities"]:
            self.assertIn(item["region"], ids, item["name"])
        names = [x["name"] for x in self.cfg["parks"] + self.cfg["cities"]]
        self.assertEqual(len(names), len(set(names)), "names must be unique")

    def test_cities_are_on_the_valley_floor(self):
        self.assertEqual({c["region"] for c in self.cfg["cities"]}, {"valley"})
        for c in self.cfg["cities"]:
            self.assertRegex(c["census_geoid"], r"^06\d{5}$")

    def test_cut_lines(self):
        lines = {c["name"]: c["east_of"] for c in self.cfg["cities"]}
        self.assertTrue(set(lines.values()) <= {"I-280", "Foothill Boulevard"})
        self.assertEqual(lines["Cupertino"], "Foothill Boulevard")
        for name in ("Palo Alto", "Mountain View", "Sunnyvale", "Los Altos"):
            self.assertEqual(lines[name], "I-280", name)


class GeoJsonTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load("regions.json")

    def check_collection(self, filename, items):
        fc = load(filename)
        self.assertEqual(fc["type"], "FeatureCollection")
        names = [f["properties"]["name"] for f in fc["features"]]
        self.assertEqual(sorted(names), sorted(x["name"] for x in items), f"{filename}: one feature per entry in regions.json")
        region_of = {x["name"]: x["region"] for x in items}
        for f in fc["features"]:
            name = f["properties"]["name"]
            self.assertEqual(f["type"], "Feature")
            self.assertEqual(f["properties"]["region"], region_of[name], name)
            self.assertTrue(f["properties"].get("source"), name)
            g = f["geometry"]
            self.assertIn(g["type"], ("Polygon", "MultiPolygon"), name)
            if g["type"] == "MultiPolygon":
                self.assertGreaterEqual(len(g["coordinates"]), 2, f"{name}: a MultiPolygon has several pieces")
            for ring in rings(g):
                self.assertGreaterEqual(len(ring), 4, name)
                self.assertEqual(ring[0], ring[-1], f"{name}: rings must be closed")
                for lon, lat in ring:
                    self.assertTrue(AREA[1] < lon < AREA[3] and AREA[0] < lat < AREA[2], f"{name}: {lon},{lat} outside the area")
                    for v in (lon, lat):
                        self.assertLessEqual(len(repr(v).split(".")[-1]), 5, f"{name}: {v} has more than 5 decimals")

    def test_parks(self):
        self.check_collection("parks.geojson", self.cfg["parks"])

    def test_cities(self):
        self.check_collection("cities.geojson", self.cfg["cities"])

    def test_trimmed_cities_keep_the_flat_part(self):
        cities = {f["properties"]["name"]: f["geometry"] for f in load("cities.geojson")["features"]}
        cupertino = cities["Cupertino"]
        self.assertTrue(10 < area_km2(cupertino) < 30, f"Cupertino is {area_km2(cupertino):.1f} km2")
        self.assertTrue(contains(cupertino, -122.0293, 37.3197), "Cupertino city hall must be kept")
        self.assertFalse(contains(cupertino, -122.0900, 37.3150), "the hills west of Foothill Boulevard must be dropped")
        palo_alto = cities["Palo Alto"]
        self.assertTrue(contains(palo_alto, -122.1430, 37.4443), "downtown Palo Alto must be kept")
        self.assertFalse(contains(palo_alto, -122.1800, 37.3600), "Foothills Park must not be in Palo Alto's area")
        # No city area overlaps a park much: no park's middle point lies inside a city area.
        for park in load("parks.geojson")["features"]:
            ring = (park["geometry"]["coordinates"] if park["geometry"]["type"] == "Polygon" else park["geometry"]["coordinates"][0])[0]
            lon, lat = sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
            for name, g in cities.items():
                if contains(park["geometry"], lon, lat):
                    self.assertFalse(contains(g, lon, lat), f"{park['properties']['name']} overlaps {name}")

    def test_sizes(self):
        sizes = {n: (DATA / n).stat().st_size for n in ("regions.json", "parks.geojson", "cities.geojson")}
        for name, size in sizes.items():
            self.assertLess(size, MAX_FILE, name)
        self.assertLess(sum(sizes.values()), MAX_TOTAL)

    def test_build_script_is_not_in_any_active_workflow(self):
        # The boundary data is static: no workflow on this branch builds it.
        for wf in (ROOT / ".github" / "workflows").glob("*.y*ml"):
            self.assertNotIn("build_boundaries", wf.read_text(), wf.name)
        # The kept copy (in tools/, where GitHub ignores it) has no schedule and no run button.
        template = (ROOT / "tools" / "build-boundaries.workflow.yml").read_text()
        self.assertIsNone(re.search(r"^\s*schedule:", template, re.M))
        self.assertNotIn("workflow_dispatch", template)

if __name__ == "__main__":
    unittest.main()
