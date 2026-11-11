"""Offline tests for the forecast step. Run with: python -m unittest

No test talks to the National Weather Service.
"""
import io
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import collect
import forecast
import rainfall as rf
from test_rainfall import FakeClock, FakeResponse


def grid(rain, chance=None, updated="2026-11-11T21:36:59+00:00"):
    """A made-up /gridpoints answer. `rain` and `chance` are [(validTime, value)]."""
    return {"properties": {
        "updateTime": updated,
        "quantitativePrecipitation": {"uom": "wmoUnit:mm", "values": [{"validTime": t, "value": v} for t, v in rain]},
        "probabilityOfPrecipitation": {"uom": "wmoUnit:percent", "values": [{"validTime": t, "value": v} for t, v in (chance or [])]},
    }}


def point(office="MTR", x=94, y=86):
    return {"properties": {"gridId": office, "gridX": x, "gridY": y}}


GAUGES = [
    {"id": "6121", "name": "Mountain View", "lat": "37.3943", "lon": "-122.06"},
    {"id": "6140", "name": "Rancho San Antonio", "lat": "37.33", "lon": "-122.118"},
]
NOW = datetime(2026, 11, 11, 14, 20, tzinfo=forecast.LOCAL)


class DaySummingTests(unittest.TestCase):
    def test_intervals(self):
        s, e = forecast.parse_interval("2026-11-13T18:00:00+00:00/PT6H")
        self.assertEqual((e - s).total_seconds(), 6 * 3600)
        s, e = forecast.parse_interval("2026-11-11T15:00:00+00:00/P1DT6H")
        self.assertEqual((e - s).total_seconds(), 30 * 3600)

    def test_blocks_are_counted_for_the_local_day_holding_most_of_their_hours(self):
        # 06-12 UTC is 10 PM - 4 AM Pacific (PST): 2 hours on Nov 12, 4 hours on Nov 13 -> Nov 13.
        props = grid([
            ("2026-11-12T08:00:00+00:00/PT6H", 2.54),   # midnight - 6 AM Nov 12 (PST)
            ("2026-11-12T14:00:00+00:00/PT16H", 0),     # 6 AM - 10 PM Nov 12
            ("2026-11-13T06:00:00+00:00/PT6H", 10.414), # 10 PM Nov 12 - 4 AM Nov 13 -> counted on Nov 13
        ])["properties"]
        days = forecast.summarize(props, date(2026, 11, 12))
        self.assertEqual(days[0]["date"], "2026-11-12")
        self.assertAlmostEqual(days[0]["rain_in"], 0.1)   # 2.54 mm
        self.assertAlmostEqual(days[1]["rain_in"], 0.41)  # 10.414 mm, counted on Nov 13
        self.assertEqual(days[1]["periods"][0]["start"], "2026-11-12T22:00-08:00")

    def test_daylight_saving_days(self):
        # Clocks go back on Nov 1, 2026 (a 25-hour day) and forward on Mar 14, 2027 (a 23-hour day).
        for day, hours, start_utc in [(date(2026, 11, 1), 25, "2026-11-01T07:00:00+00:00"),
                                      (date(2027, 3, 14), 23, "2027-03-14T08:00:00+00:00")]:
            props = grid([(f"{start_utc}/PT{hours}H", 25.4)], [(f"{start_utc}/PT{hours}H", 40)])["properties"]
            d = forecast.summarize(props, day)[0]
            self.assertEqual(d["hours_covered"], hours)
            self.assertTrue(d["complete"])
            self.assertEqual(d["rain_in"], 1.0)
            self.assertEqual(d["chance_max_pct"], 40)

    def test_partial_days_and_missing_amounts(self):
        props = grid([
            ("2026-11-11T15:00:00+00:00/PT3H", 0),      # forecast starts 7 AM Nov 11
            ("2026-11-11T18:00:00+00:00/PT6H", None),   # NWS gave no amount
            ("2026-11-12T00:00:00+00:00/PT6H", 1.27),
        ], [("2026-11-11T15:00:00+00:00/PT9H", 20), ("2026-11-12T00:00:00+00:00/PT6H", 55)])["properties"]
        days = forecast.summarize(props, date(2026, 11, 11))
        today = days[0]
        self.assertIsNone(today["rain_in"])  # unknown, not 0
        self.assertFalse(today["complete"])
        self.assertEqual(today["hours_covered"], 15)  # 7 AM to 10 PM
        self.assertEqual(today["chance_max_pct"], 55)
        self.assertEqual([p["rain_in"] for p in today["periods"]], [0, None, 0.05])
        self.assertIsNone(days[3]["rain_in"])  # beyond the forecast: no periods, so unknown
        self.assertFalse(days[3]["complete"])
        self.assertIsNone(days[3]["chance_max_pct"])
        self.assertEqual(len(days), 7)


class StepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = mock.patch.object(rf, "DATA_DIR", Path(self.tmp.name) / "data")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.log = lambda *a: None

    def fake_nws(self, fail=False):
        calls = []

        def fetch(url):
            calls.append(url)
            if fail:
                raise RuntimeError("gave up after 3 tries (HTTP Error 500)")
            if "/points/" in url:
                return point()  # both test gauges are in the same grid cell
            return grid([("2026-11-11T22:00:00+00:00/PT6H", 2.54)], [("2026-11-11T22:00:00+00:00/PT6H", 30)])
        return fetch, calls

    def test_grid_lookup_once_and_shared_cells_fetched_once(self):
        fetch, calls = self.fake_nws()
        out = forecast.update(GAUGES, NOW, fetch, self.log)
        self.assertEqual(sum("/points/" in c for c in calls), 2)
        self.assertEqual(sum("/gridpoints/" in c for c in calls), 1)  # one cell for both gauges
        self.assertEqual(out["gauges"]["6140"]["grid"], {"office": "MTR", "x": 94, "y": 86})
        self.assertEqual(out["gauges"]["6121"]["nws_updated"], "2026-11-11T21:36:59+00:00")
        self.assertIsNone(out["last_error"])
        fetch, calls = self.fake_nws()
        forecast.update(GAUGES, NOW, fetch, self.log)
        self.assertEqual(sum("/points/" in c for c in calls), 0)  # saved lookups reused

    def test_failure_keeps_previous_forecast_and_records_error(self):
        fetch, _ = self.fake_nws()
        forecast.update(GAUGES, NOW, fetch, self.log)
        before = json.loads(forecast.forecast_file().read_text())
        fetch, calls = self.fake_nws(fail=True)
        later = datetime(2026, 11, 11, 15, 20, tzinfo=forecast.LOCAL)
        out = forecast.update(GAUGES, later, fetch, self.log)  # must not raise
        self.assertEqual(len(calls), 1)  # stopped asking NWS after the first failure
        saved = json.loads(forecast.forecast_file().read_text())
        self.assertEqual(saved["gauges"], before["gauges"])
        self.assertEqual(saved["fetched_at"], before["fetched_at"])
        self.assertEqual(saved["last_error"]["time"], "2026-11-11T15:20:00-08:00")
        self.assertIn("HTTP Error 500", saved["last_error"]["message"])
        self.assertEqual(out["last_error"], saved["last_error"])

    def test_failure_with_no_previous_file(self):
        fetch, _ = self.fake_nws(fail=True)
        forecast.update(GAUGES, NOW, fetch, self.log)
        saved = json.loads(forecast.forecast_file().read_text())
        self.assertEqual(saved["gauges"], {})
        self.assertIn("gave up", saved["last_error"]["message"])

    def test_unexpected_answer_does_not_raise(self):
        forecast.update(GAUGES, NOW, lambda url: {"properties": {}}, self.log)
        self.assertIsNotNone(json.loads(forecast.forecast_file().read_text())["last_error"])

    def test_file_not_rewritten_when_only_fetch_time_changes(self):
        fetch, _ = self.fake_nws()
        forecast.update(GAUGES, NOW, fetch, self.log)
        first = forecast.forecast_file().read_text()
        fetch, _ = self.fake_nws()
        forecast.update(GAUGES, datetime(2026, 11, 11, 15, 20, tzinfo=forecast.LOCAL), fetch, self.log)
        self.assertEqual(forecast.forecast_file().read_text(), first)

    def test_requests_are_polite(self):
        clock, seen = FakeClock(), []

        def opener(req, timeout):
            seen.append(req.get_header("User-agent"))
            raise TimeoutError("timed out")

        with self.assertRaises(RuntimeError):
            forecast.get_json(f"{forecast.NWS}/points/1,2", rf.Pacer(5, clock.sleep, clock.clock),
                              log=self.log, sleep=clock.sleep, opener=opener)
        self.assertEqual(len(seen), 3)  # first try + at most 2 retries
        self.assertEqual(clock.sleeps, [30, 120])
        self.assertIn("github.com/fdesilvaleon/rainfall-dashboard", seen[0])

    def test_forecast_failure_never_stops_the_rainfall_data(self):
        gauges = [{"id": "2", "name": "B"}]
        with mock.patch.object(rf, "load_gauges", return_value=gauges), \
             mock.patch.object(rf, "HISTORY_DIR", rf.DATA_DIR / "history"), \
             mock.patch.object(rf, "STATS_FILE", rf.DATA_DIR / "stats.json"), \
             mock.patch.object(rf, "fetch_rain", lambda *a: []), \
             mock.patch.object(rf, "now_pst", return_value=datetime(2026, 11, 11, 14, 20)), \
             mock.patch.object(forecast, "update", side_effect=RuntimeError("boom")), \
             mock.patch("sys.stderr", io.StringIO()), mock.patch("sys.stdout", io.StringIO()):
            collect.main()  # no exception, no exit
        self.assertTrue((rf.DATA_DIR / "2.csv").exists())
        self.assertTrue((rf.DATA_DIR / "stats.json").exists())


if __name__ == "__main__":
    unittest.main()
