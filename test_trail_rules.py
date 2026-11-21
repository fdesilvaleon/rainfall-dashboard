"""Checks the trail-rating rules that live in index.html (the TRAIL settings and the rating functions).

The functions are taken from index.html as they are and run with Node. If Node isn't installed, these tests are
skipped (the other tests use only the standard library).
"""
import json
import random
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
HTML = (ROOT / "index.html").read_text()
FUNCTIONS = ["rateWetness", "rateBlockRain", "rateComfort", "blockRating", "ratingPosition", "positionText",
             "watchForLine", "roadWetAnchor", "markerLeftPercent"]


def extract():
    """The TRAIL settings block and the rating functions, as JavaScript source."""
    m = re.search(r"^const TRAIL = \{.*?^\};$", HTML, re.M | re.S)
    parts = [m.group(0)]
    for name in FUNCTIONS:
        f = re.search(rf"^function {name}\(.*?^\}}$", HTML, re.M | re.S)
        assert f, f"function {name} not found in index.html"
        parts.append(f.group(0))
    return "\n".join(parts)


def run_js(expr_by_key):
    """Evaluate JavaScript expressions (with the rules loaded); returns {key: value}."""
    body = extract() + "\nconst __out = {};\n" + "".join(
        f"__out[{json.dumps(k)}] = (() => {{ {v} }})();\n" for k, v in expr_by_key.items()) + "console.log(JSON.stringify(__out));"
    res = subprocess.run(["node"], input=body, capture_output=True, text=True, timeout=60)
    if res.returncode:
        raise AssertionError(res.stderr)
    return json.loads(res.stdout)


@unittest.skipUnless(shutil.which("node"), "Node isn't installed")
class TrailRulesTests(unittest.TestCase):
    def test_settings(self):
        out = run_js({
            "ratings": "return TRAIL.RATINGS;",
            "wet": "return TRAIL.WETNESS;",
            "season": "return TRAIL.SEASON_ADJUST.map(s => [s.BELOW === Infinity ? 'inf' : s.BELOW, s.running, s.biking]);",
            "road": "return TRAIL.ROAD_WET;",
            "watch": "return TRAIL.WATCH_FOR;",
        })
        self.assertEqual(out["ratings"], ["Great", "Good", "Fair", "Poor"])
        self.assertEqual(out["wet"], {"running": [0.40, 1.50, 3.00], "biking": [0.02, 0.05, 0.15]})
        self.assertEqual(out["season"], [[2.0, 1.0, 2.5], [5.0, 1.0, 1.5], ["inf", 1.0, 1.0]])  # unchanged
        self.assertEqual(out["road"]["ANCHOR"], "end")
        self.assertEqual(out["watch"], {"running": "Expect puddles in shaded low spots and slick clay on steep sections."})

    def test_fede_rancho_runs(self):
        # Foothills running, ground wetness only (as for past blocks): Sun Nov 15 felt very good, Fri Nov 20 good to very good.
        cases = {"nov15_10am": 0.39, "nov15_4pm": 0.24, "nov20_10am": 1.95, "nov20_4pm": 1.36}
        out = run_js({k: f"const p = ratingPosition({{ wetness: {v} }}, 'running', 1, true); "
                         f"return [blockRating({{ wetness: {v} }}, 'running', 1, true).rating, p.position];" for k, v in cases.items()})
        self.assertEqual(out["nov15_10am"][0], 0)   # Great
        self.assertEqual(out["nov15_4pm"][0], 0)    # Great
        self.assertEqual(out["nov20_10am"][0], 2)   # Fair
        self.assertEqual(out["nov20_4pm"][0], 1)    # Good
        self.assertAlmostEqual(out["nov15_10am"][1], 0.39 / 0.40)               # 0.975
        self.assertAlmostEqual(out["nov15_4pm"][1], 0.24 / 0.40)                # 0.60
        self.assertAlmostEqual(out["nov20_10am"][1], 2 + (1.95 - 1.50) / 1.50)  # 2.30
        self.assertAlmostEqual(out["nov20_4pm"][1], 1 + (1.36 - 0.40) / 1.10)   # 1.873

    def test_position_examples(self):
        out = run_js({
            "rain_poor": "return ratingPosition({ wetness: 0, rain: 0.20, feels: 55 }, 'running').position;",
            "rain_mid_bike": "return ratingPosition({ wetness: 0, rain: 0.08, feels: 60 }, 'biking').position;",
            "cold_poor_cap": "return ratingPosition({ wetness: 0, rain: 0, feels: 15 }, 'running').position;",
            "cold_fair_edge": "return ratingPosition({ wetness: 0, rain: 0, feels: 30 }, 'running').position;",
            "hot_good": "return ratingPosition({ wetness: 0, rain: 0, feels: 73 }, 'running').position;",
            "wet_poor_cap": "return ratingPosition({ wetness: 9 }, 'running').position;",
            "season_mult": "return ratingPosition({ wetness: 0.06 }, 'biking', 2.5).position;",
            "road_poor": "return ratingPosition({ rain: 0, feels: 55, roadPoor: 0.20, roadFair: 0.30 }, 'running', 1, false).position;",
            "road_fair": "return ratingPosition({ rain: 0, feels: 55, roadPoor: 0.10, roadFair: 0.085 }, 'running', 1, false).position;",
            "road_none": "return ratingPosition({ rain: 0, feels: 55, roadPoor: 0, roadFair: 0.01 }, 'running', 1, false).position;",
            "valley_ignores_wetness": "return ratingPosition({ wetness: 5, rain: 0, feels: 55 }, 'running', 1, false).position;",
            "no_rating": "return ratingPosition({ wetness: null, rain: 0, feels: 55 }, 'running').position;",
        })
        self.assertAlmostEqual(out["rain_poor"], 3 + 0.05 / 0.15)
        self.assertAlmostEqual(out["rain_mid_bike"], 2 + (0.08 - 0.01) / 0.14)
        self.assertAlmostEqual(out["cold_poor_cap"], 3.95)
        self.assertAlmostEqual(out["cold_fair_edge"], 2.99)  # at the Fair range's worse edge, still Fair
        self.assertAlmostEqual(out["hot_good"], 1 + (73 - 68) / (78 - 68))
        self.assertAlmostEqual(out["wet_poor_cap"], 3.95)
        self.assertAlmostEqual(out["season_mult"], 1 + (0.06 - 0.05) / (0.125 - 0.05))  # limits x 2.5
        self.assertAlmostEqual(out["road_poor"], 3 + 0.05 / 0.15)
        self.assertAlmostEqual(out["road_fair"], 2 + (0.09 - 0.02) / 0.13)  # rounded to 0.09
        self.assertAlmostEqual(out["road_none"], 0.5)
        self.assertLess(out["valley_ignores_wetness"], 1)
        self.assertIsNone(out["no_rating"])

    def test_position_whole_part_is_the_rating(self):
        # Many random blocks, for running and biking, trail regions (with season multipliers) and the Valley floor.
        rnd = random.Random(7)
        cases = []
        for _ in range(3000):
            sport = rnd.choice(["running", "biking"])
            paved = rnd.random() < 0.35
            inp = {"wetness": None if rnd.random() < 0.1 else round(rnd.uniform(0, 4), 3),
                   "rain": None if rnd.random() < 0.15 else round(rnd.choice([0, rnd.uniform(0, 0.02), rnd.uniform(0, 0.5)]), 3),
                   "feels": None if rnd.random() < 0.15 else round(rnd.uniform(5, 105), 1)}
            if paved:
                inp["roadFair"] = None if rnd.random() < 0.1 else round(rnd.uniform(0, 0.4), 3)
                inp["roadPoor"] = None if rnd.random() < 0.1 else round(rnd.uniform(0, 0.3), 3)
            cases.append([inp, sport, rnd.choice([1, 1.5, 2.5]), not paved])
        out = run_js({"all": f"return {json.dumps(cases)}.map(([i, s, m, w]) => "
                             f"[blockRating(i, s, m, w).rating, ratingPosition(i, s, m, w).position]);"})
        for (inp, sport, mult, wet), (rating, pos) in zip(cases, out["all"]):
            with self.subTest(inp=inp, sport=sport, mult=mult, wetness=wet):
                if rating is None:
                    self.assertIsNone(pos)
                else:
                    self.assertEqual(int(pos // 1), rating)
                    self.assertLess(pos, 4)

    def test_position_words(self):
        out = run_js({k: f"return positionText({v});" for k, v in
                      {"g0": 0.2, "g1": 0.99, "good_better": 1.1, "good_mid": 1.5, "good_worse": 1.8,
                       "fair_better": 2.2, "fair_mid": 2.5, "fair_worse": 2.9, "poor": 3.4}.items()})
        self.assertEqual(out, {"g0": "Great", "g1": "Great", "good_better": "Good, close to Great", "good_mid": "Good",
                               "good_worse": "Good, close to Fair", "fair_better": "Fair, close to Good", "fair_mid": "Fair",
                               "fair_worse": "Fair, close to Poor", "poor": "Poor"})

    def test_watch_for_rule(self):
        trail = "{ useWetness: true }"
        paved = "{ useWetness: false, roadWet: true }"
        out = run_js({
            "running_good": f"return watchForLine({trail}, 'running', 1);",
            "running_poor": f"return watchForLine({trail}, 'running', 3);",
            "running_great": f"return watchForLine({trail}, 'running', 0);",
            "running_unknown": f"return watchForLine({trail}, 'running', null);",
            "biking": f"return watchForLine({trail}, 'biking', 3);",
            "valley": f"return watchForLine({paved}, 'running', 3);",
        })
        text = "Expect puddles in shaded low spots and slick clay on steep sections."
        self.assertEqual(out["running_good"], text)
        self.assertEqual(out["running_poor"], text)
        for k in ("running_great", "running_unknown", "biking", "valley"):
            self.assertIsNone(out[k], k)

    def test_marker_runs_poor_left_to_great_right(self):
        out = run_js({k: f"return markerLeftPercent({v});" for k, v in {"pos05": 0.5, "poor": 3.95, "great": 0.0, "fair": 2.5}.items()})
        self.assertAlmostEqual(out["pos05"], 87.5)
        self.assertAlmostEqual(out["poor"], 1.25)
        self.assertAlmostEqual(out["great"], 100.0)
        self.assertAlmostEqual(out["fair"], 37.5)
        # The marker stays inside its rating's quarter of the bar: Great in the right quarter, Poor in the left one.
        for pos in (0.0, 0.5, 0.99, 1.0, 1.5, 1.99, 2.0, 2.99, 3.0, 3.95):
            left = run_js({"x": f"return markerLeftPercent({pos});"})["x"]
            quarter_from_right = int(pos)
            self.assertTrue(100 - 25 * (quarter_from_right + 1) <= left <= 100 - 25 * quarter_from_right, (pos, left))

    def test_bar_and_legend_order_in_the_page(self):
        css = re.search(r"\.pos-bar \{[^}]*\}", HTML).group(0)
        stops = re.findall(r"var\(--r(\d)-bg\)", css)
        self.assertEqual(stops, ["3", "3", "2", "1", "0", "0"])  # Poor on the left, Great on the right
        self.assertIn("for (let k = TRAIL.RATINGS.length - 1; k >= 0; k--)", HTML)  # legend worst to best

    def test_road_wet_anchor(self):
        out = run_js({a: f"TRAIL.ROAD_WET.ANCHOR = '{a}'; return roadWetAnchor(100, 106);" for a in ("start", "mid", "end")})
        self.assertEqual(out, {"start": 100, "mid": 103, "end": 106})


class GreatNotOptimalTests(unittest.TestCase):
    """The top rating is called Great everywhere people can see it."""

    def test_page_text(self):
        visible = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", "", HTML, flags=re.S)
        self.assertNotRegex(visible.lower(), "optimal")
        self.assertIn('RATINGS: ["Great", "Good", "Fair", "Poor"]', HTML)

    def test_readme(self):
        self.assertNotRegex((ROOT / "README.md").read_text().lower(), "optimal")


if __name__ == "__main__":
    unittest.main()
