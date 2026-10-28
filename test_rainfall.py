"""Offline tests for the collector. Run with: python -m unittest

No test talks to the district's API.
"""
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import backfill
import collect
import rainfall as rf

TIP = 0.0393701  # one tip of the gauge's bucket (1 mm)


def payload(gid, rows, comments=("ok",)):
    return {"comments": list(comments), "metadata": {"id": gid, "name": f"Gauge ({gid})"},
            "data": [{"id": gid, "timestamp": t, "value": v, "rating": None} for t, v in rows]}


class FakeClock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds

    def clock(self):
        return self.now


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TempDataDir(unittest.TestCase):
    """Points the collector at a temporary folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        patches = [mock.patch.object(rf, "DATA_DIR", root / "data"),
                   mock.patch.object(rf, "HISTORY_DIR", root / "data" / "history"),
                   mock.patch.object(rf, "STATS_FILE", root / "data" / "stats.json")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)


class TimeTests(unittest.TestCase):
    def test_values_belong_to_the_hour_and_day_they_end_in(self):
        self.assertEqual(rf.hour_of(datetime(2026, 10, 22, 2, 0)), datetime(2026, 10, 22, 1, 0))
        self.assertEqual(rf.hour_of(datetime(2026, 10, 22, 2, 15)), datetime(2026, 10, 22, 2, 0))
        self.assertEqual(rf.hour_of(datetime(2026, 10, 23, 0, 0)).date(), date(2026, 10, 22))

    def test_water_years(self):
        self.assertEqual(rf.water_year(date(2025, 10, 1)), 2026)
        self.assertEqual(rf.water_year(date(2026, 9, 30)), 2026)
        self.assertEqual(rf.wy_label(2026), "2025-26")


class ApiTests(unittest.TestCase):
    def test_parse_converts_to_pst_and_rejects_error_answers(self):
        readings = rf.parse_response(payload("6121", [("2026-10-22T02:00:00-08:00", TIP), ("2026-07-01T03:00:00-07:00", TIP)]), "6121")
        self.assertEqual([t for t, _ in readings], [datetime(2026, 10, 22, 2, 0), datetime(2026, 7, 1, 2, 0)])
        error = {"comments": ["Could not retrieve data with specified parameters."], "metadata": {"id": None}, "data": []}
        with self.assertRaises(ValueError):
            rf.parse_response(error, "6121")

    def test_dry_period_is_not_an_error(self):
        self.assertEqual(rf.parse_response(payload("6121", []), "6121"), [])

    def test_request_is_polite(self):
        clock = FakeClock()
        seen = []

        def opener(req, timeout):
            seen.append((req.full_url, req.get_header("User-agent")))
            return FakeResponse(json.dumps(payload("6121", [])).encode())

        pacer = rf.Pacer(5, sleep=clock.sleep, clock=clock.clock)
        rf.fetch_rain("6121", date(2026, 10, 26), date(2026, 10, 28), pacer, sleep=clock.sleep, opener=opener)
        rf.fetch_rain("6121", date(2026, 10, 26), date(2026, 10, 28), pacer, sleep=clock.sleep, opener=opener)
        self.assertEqual(seen[0][0], "https://alertdata.valleywater.org/SensorData/6121/range/2026-10-26/2026-10-28?skipZeroValues=true")
        self.assertIn("github.com/fdesilvaleon/rainfall-dashboard", seen[0][1])
        self.assertEqual(clock.sleeps, [5])  # the second call waited 5 seconds

    def test_retries_twice_with_growing_waits_then_gives_up(self):
        clock, calls = FakeClock(), []

        def opener(req, timeout):
            calls.append(1)
            raise TimeoutError("timed out")

        with self.assertRaises(RuntimeError):
            rf.fetch_rain("6121", date(2026, 10, 26), date(2026, 10, 28), rf.Pacer(5, clock.sleep, clock.clock),
                          log=lambda *a: None, sleep=clock.sleep, opener=opener)
        self.assertEqual(len(calls), 3)
        self.assertEqual(clock.sleeps, [30, 120])

    def test_retry_succeeds(self):
        clock, answers = FakeClock(), [OSError("reset"), payload("6121", [("2026-10-22T02:00:00-08:00", TIP)])]

        def opener(req, timeout):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return FakeResponse(json.dumps(a).encode())

        readings = rf.fetch_rain("6121", date(2026, 10, 22), date(2026, 10, 22), rf.Pacer(5, clock.sleep, clock.clock),
                                 log=lambda *a: None, sleep=clock.sleep, opener=opener)
        self.assertEqual(len(readings), 1)
        self.assertEqual(clock.sleeps, [30])


class FileTests(TempDataDir):
    def test_hourly_sums_and_season_total_restarts_on_oct_1(self):
        readings = [(datetime(2026, 9, 30, 23, 30), TIP), (datetime(2026, 10, 1, 0, 0), TIP),  # both Sep 30, 23:00 hour
                    (datetime(2026, 10, 1, 0, 15), TIP), (datetime(2026, 10, 1, 1, 0), TIP),   # Oct 1, 00:00 hour
                    (datetime(2026, 10, 1, 1, 15), -1.0)]                                       # impossible: ignored
        rain = rf.hourly_totals(readings, datetime(2026, 9, 30, 22), datetime(2026, 10, 1, 1))
        path = Path(self.tmp.name) / "h.csv"
        rf.write_hourly(path, rain, datetime(2026, 9, 30, 22), datetime(2026, 10, 1, 1))
        rows = path.read_text().splitlines()
        self.assertEqual(rows[1:], ["2026-09-30 22:00,0.00,0", "2026-09-30 23:00,0.08,0.0787",
                                    "2026-10-01 00:00,0.08,0.0787", "2026-10-01 01:00,0.08,0"])
        self.assertAlmostEqual(rf.read_hourly(path)[datetime(2026, 10, 1, 0)], 0.0787)

    def test_hours_without_data_are_blank(self):
        path = Path(self.tmp.name) / "h.csv"
        rf.write_hourly(path, {datetime(2026, 10, 1, 0): TIP}, datetime(2026, 10, 1, 0), datetime(2026, 10, 1, 1))
        self.assertEqual(path.read_text().splitlines()[2], "2026-10-01 01:00,,")
        self.assertIsNone(rf.read_hourly(path)[datetime(2026, 10, 1, 1)])


class CollectTests(TempDataDir):
    gauge = {"id": "6121", "name": "Mountain View"}

    def run_collect(self, now, rows):
        calls = []

        def fetch(gid, start, end):
            calls.append((start, end))
            return [(t, v) for t, v in rows if start <= rf.hour_of(t).date() <= end]

        collect.update_gauge(self.gauge, now, fetch)
        return calls

    def test_fetches_48_hours_merges_without_duplicates(self):
        now = datetime(2026, 10, 28, 14, 20)
        backfill.build_gauge("6121", [(datetime(2015, 1, 5, 3, 0), TIP), (datetime(2026, 10, 22, 2, 0), TIP)], datetime(2026, 10, 27, 10, 5))
        calls = self.run_collect(now, [(datetime(2026, 10, 28, 9, 15), TIP), (datetime(2026, 10, 28, 9, 30), TIP)])
        self.assertEqual(calls, [(date(2026, 10, 26), date(2026, 10, 28))])
        rain = rf.read_hourly(rf.DATA_DIR / "6121.csv")
        self.assertEqual(len(rain), len(set(rain)))
        self.assertEqual(min(rain), datetime(2025, 10, 1, 0))  # current and previous water year
        self.assertEqual(max(rain), datetime(2026, 10, 28, 14))
        self.assertAlmostEqual(rain[datetime(2026, 10, 28, 9)], 0.0787)
        self.assertAlmostEqual(rain[datetime(2026, 10, 22, 1)], 0.0394)  # older history kept
        daily = rf.read_daily(rf.HISTORY_DIR / "6121_daily.csv")
        self.assertAlmostEqual(daily[date(2026, 10, 28)], 0.0787)
        self.assertAlmostEqual(daily[date(2026, 10, 22)], 0.0394)
        self.assertIn("2026-10-28 14:00,0.12,0", (rf.DATA_DIR / "6121.csv").read_text())
        # Running again changes nothing.
        before = (rf.DATA_DIR / "6121.csv").read_text()
        self.run_collect(now, [(datetime(2026, 10, 28, 9, 15), TIP), (datetime(2026, 10, 28, 9, 30), TIP)])
        self.assertEqual((rf.DATA_DIR / "6121.csv").read_text(), before)

    def test_fills_recent_gaps_with_the_same_single_request(self):
        backfill.build_gauge("6121", [(datetime(2015, 1, 5, 3, 0), TIP)], datetime(2026, 10, 20, 8, 5))
        calls = self.run_collect(datetime(2026, 10, 28, 14, 20), [(datetime(2026, 10, 21, 5, 0), TIP)])
        self.assertEqual(calls, [(date(2026, 10, 20), date(2026, 10, 28))])
        rain = rf.read_hourly(rf.DATA_DIR / "6121.csv")
        self.assertTrue(all(rain[h] is not None for h in rf.hours_between(datetime(2026, 10, 20, 0), datetime(2026, 10, 28, 14))))
        self.assertAlmostEqual(rain[datetime(2026, 10, 21, 4)], 0.0394)

    def test_one_failing_gauge_does_not_stop_the_others(self):
        gauges = [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]

        def fake_fetch(gid, start, end, pacer):
            if gid == "1":
                raise RuntimeError("gave up after 3 tries")
            return []

        with mock.patch.object(rf, "load_gauges", return_value=gauges), \
             mock.patch.object(rf, "fetch_rain", fake_fetch), \
             mock.patch.object(rf, "now_pst", return_value=datetime(2026, 10, 28, 14, 20)), \
             mock.patch("sys.stderr", io.StringIO()), mock.patch("sys.stdout", io.StringIO()):
            collect.main()
        self.assertFalse((rf.DATA_DIR / "1.csv").exists())
        self.assertTrue((rf.DATA_DIR / "2.csv").exists())
        self.assertTrue(rf.STATS_FILE.exists())


class StatsTests(unittest.TestCase):
    def history(self, first_day, last_day, per_day, gaps=()):
        """Daily rain: `per_day` in on every day; days before first_day or in gaps are blank (no data)."""
        out = {}
        for d in rf.days_between(date(2018, 10, 1), last_day):
            out[d] = None if d < first_day or d in gaps else per_day
        return out

    def test_complete_years_averages_and_flags(self):
        today = date(2026, 10, 28)  # current water year 2026-27, so 2011-12 .. 2025-26 are candidates
        gaps = set(rf.days_between(date(2023, 1, 1), date(2023, 1, 31)))  # 31 days missing in 2022-23 (91.5%)
        daily = {
            "A": self.history(date(2018, 10, 1), today, 0.1, gaps),
            "B": self.history(date(2020, 10, 1), today, 0.2),
        }
        stats = rf.compute_stats([{"id": "A", "name": "A"}, {"id": "B", "name": "B"}], daily, today)
        a, b = stats["gauges"]["A"], stats["gauges"]["B"]
        self.assertEqual(stats["current_water_year"], "2026-27")
        self.assertEqual(len(stats["days"]), 366)
        self.assertEqual(a["water_years_used"], ["2018-19", "2019-20", "2020-21", "2021-22", "2023-24", "2024-25", "2025-26"])
        self.assertIn({"water_year": "2022-23", "coverage_percent": 91.5, "reason": "only 92% of days have data"}, a["excluded_years"])
        self.assertFalse(a["limited_history"])
        self.assertEqual(a["date_range"], {"from": "2018-10-01", "to": "2026-09-30"})
        self.assertEqual(b["years_used"], 6)  # 2020-21 .. 2025-26
        self.assertFalse(b["limited_history"])
        four = rf.compute_stats([{"id": "B", "name": "B"}], {"B": self.history(date(2022, 10, 1), today, 0.2)}, today)
        self.assertEqual(four["gauges"]["B"]["years_used"], 4)
        self.assertTrue(four["gauges"]["B"]["limited_history"])  # fewer than 5 complete years
        # Feb 29: in years without one it repeats Feb 28.
        feb28, feb29 = stats["days"].index("02-28"), stats["days"].index("02-29")
        self.assertEqual(a["last_year"][feb29], a["last_year"][feb28])
        self.assertAlmostEqual(a["last_year"][-1], round(365 * 0.1, 2))
        self.assertAlmostEqual(a["average"][0], 0.1)
        self.assertGreaterEqual(a["max"][-1], a["average"][-1])
        self.assertLessEqual(a["min"][-1], a["average"][-1])
        # The current water year never counts.
        self.assertNotIn("2026-27", a["water_years_used"])

    def test_dry_spell_while_neighbors_got_rain_is_a_suspected_outage(self):
        days = list(rf.days_between(date(2025, 1, 1), date(2025, 1, 10)))
        wet = {d: 1.0 for d in days}
        broken = {d: (0.0 if date(2025, 1, 3) <= d <= date(2025, 1, 6) else 1.0) for d in days}
        found = rf.suspected_outages("X", {"X": broken, "Y": wet, "Z": wet}, days[0], days[-1])
        self.assertEqual(found, [(date(2025, 1, 3), date(2025, 1, 6))])
        # Everyone dry: a real dry spell, not an outage.
        dry = {d: 0.0 for d in days}
        self.assertEqual(rf.suspected_outages("X", {"X": dry, "Y": dry, "Z": dry}, days[0], days[-1]), [])


if __name__ == "__main__":
    unittest.main()
