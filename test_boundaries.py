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

    def test_sizes(self):
        sizes = {n: (DATA / n).stat().st_size for n in ("regions.json", "parks.geojson", "cities.geojson")}
        for name, size in sizes.items():
            self.assertLess(size, MAX_FILE, name)
        self.assertLess(sum(sizes.values()), MAX_TOTAL)

    def test_build_script_is_not_in_the_hourly_workflow(self):
        hourly = (ROOT / ".github" / "workflows" / "collect.yml").read_text()
        self.assertNotIn("build_boundaries", hourly)
        # The boundary workflow runs only on the add-boundaries branch, never on a schedule.
        boundary = (ROOT / ".github" / "workflows" / "build-boundaries.yml").read_text()
        self.assertIsNone(re.search(r"^\s*schedule:", boundary, re.M))
        self.assertNotIn("workflow_dispatch", boundary)
        self.assertIn("branches: [add-boundaries]", boundary)


if __name__ == "__main__":
    unittest.main()
