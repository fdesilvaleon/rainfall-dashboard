"""Offline tests for the forecast history. Run with: python -m unittest

No test talks to the National Weather Service or to git.
"""
import csv
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import forecast_history as fh
import rainfall as rf

LOCAL = fh.LOCAL


def day(d, rain, chance, complete=True):
    return {"date": d, "rain_in": rain, "complete": complete, "hours_covered": 24, "chance_max_pct": chance, "periods": []}


def fc(fetched, issued="2026-11-12T10:26:47+00:00", days=None, error=None):
    days = days or [day("2026-11-12", 0.0, 5, complete=False), day("2026-11-13", 0.52, 98), day("2026-11-14", None, None)]
    gauge = {"name": "G", "grid": {}, "nws_updated": issued, "fetched_at": fetched.isoformat(timespec="seconds"), "days": days}
    return {"gauges": {"6121": gauge, "6140": dict(gauge)}, "last_error": error, "fetched_at": gauge["fetched_at"]}


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=LOCAL)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = mock.patch.object(rf, "DATA_DIR", Path(self.tmp.name) / "data")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.log = lambda *a: None

    def rows(self):
        with open(fh.history_file(), newline="") as f:
            return list(csv.DictReader(f))

    def test_saves_once_per_pacific_day_from_6_am(self):
        self.assertTrue(fh.save_daily_snapshot(fc(at(2026, 11, 12, 5, 30)), at(2026, 11, 12, 5, 30), self.log).startswith("not yet"))
        self.assertFalse(fh.history_file().exists())
        now = at(2026, 11, 12, 7, 0)
        self.assertEqual(fh.save_daily_snapshot(fc(now), now, self.log), "saved 6 rows for 2026-11-12")
        later = at(2026, 11, 12, 8, 0)
        self.assertTrue(fh.save_daily_snapshot(fc(later), later, self.log).startswith("already saved"))
        self.assertEqual(len(self.rows()), 6)  # no second snapshot the same day
        tomorrow = at(2026, 11, 13, 6, 5)
        fh.save_daily_snapshot(fc(tomorrow, issued="2026-11-13T13:00:00+00:00"), tomorrow, self.log)
        self.assertEqual(len(self.rows()), 12)

    def test_columns_blanks_and_flags(self):
        now = at(2026, 11, 12, 7, 0)
        fh.save_daily_snapshot(fc(now), now, self.log)
        r = [x for x in self.rows() if x["gauge_id"] == "6121"]
        self.assertEqual(r[0], {"snapshot_time": "2026-11-12T10:26:47Z", "saved_at": "2026-11-12T15:00:00Z", "gauge_id": "6121",
                                "target_date": "2026-11-12", "lead_days": "0", "expected_in": "0", "chance_pct": "5", "partial_day": "1"})
        self.assertEqual((r[1]["expected_in"], r[1]["chance_pct"], r[1]["partial_day"], r[1]["lead_days"]), ("0.52", "98", "0", "1"))
        self.assertEqual((r[2]["expected_in"], r[2]["chance_pct"]), ("", ""))  # unknown stays blank, not 0

    def test_lead_days_across_midnight_and_daylight_saving(self):
        target = [day("2026-11-12", 0.1, 10)]
        # Issued 11:30 PM Pacific on Nov 11 (07:30 UTC Nov 12): Nov 12 is 1 day ahead.
        rows = fh.rows_for(fc(at(2026, 11, 12, 7), issued="2026-11-12T07:30:00+00:00", days=target), at(2026, 11, 12, 7))
        self.assertEqual(rows[0]["lead_days"], 1)
        # Issued 12:30 AM Pacific on Nov 12: same day.
        rows = fh.rows_for(fc(at(2026, 11, 12, 7), issued="2026-11-12T08:30:00+00:00", days=target), at(2026, 11, 12, 7))
        self.assertEqual(rows[0]["lead_days"], 0)
        # Summer (daylight saving time): 07:30 UTC Jul 1 is 12:30 AM PDT Jul 1 (it would be Jun 30 in PST).
        rows = fh.rows_for(fc(at(2026, 7, 1, 7), issued="2026-07-01T07:30:00+00:00", days=[day("2026-07-01", 0, 0)]), at(2026, 7, 1, 7))
        self.assertEqual(rows[0]["lead_days"], 0)
        # Saved at 7:00 AM PDT, which is 14:00 UTC.
        self.assertEqual(rows[0]["saved_at"], "2026-07-01T14:00:00Z")

    def test_skipped_when_stale_failed_or_missing(self):
        now = at(2026, 11, 12, 7, 0)
        for bad, why in [(None, "no forecast"), ({"gauges": {}}, "no forecast"),
                         (fc(now - timedelta(hours=7)), "more than 6 hours old"),
                         (fc(now, error={"time": "x", "message": "HTTP Error 500"}), "HTTP Error 500")]:
            result = fh.save_daily_snapshot(bad, now, self.log)
            self.assertIn(why, result)
            self.assertIn("next run will try again", result)
        self.assertFalse(fh.history_file().exists())
        # A later run the same day with a good forecast still saves.
        self.assertTrue(fh.save_daily_snapshot(fc(now + timedelta(hours=1)), now + timedelta(hours=1), self.log).startswith("saved"))

    def test_old_rows_are_never_changed(self):
        fh.save_daily_snapshot(fc(at(2026, 11, 12, 7)), at(2026, 11, 12, 7), self.log)
        before = fh.history_file().read_bytes()
        fh.save_daily_snapshot(fc(at(2026, 11, 13, 7), issued="2026-11-13T12:00:00+00:00"), at(2026, 11, 13, 7), self.log)
        after = fh.history_file().read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertGreater(len(after), len(before))

    def test_seed_picks_first_snapshot_from_6_am(self):
        good = fc(at(2026, 11, 12, 3, 10))
        commits = [("a", at(2026, 11, 12, 3, 10), good),           # before 6 AM
                   ("b", at(2026, 11, 12, 7, 0), good),            # first run from 6 AM: chosen
                   ("c", at(2026, 11, 12, 11, 0), fc(at(2026, 11, 12, 11)))]
        commit, when, chosen, reason = fh.pick_seed(commits, date(2026, 11, 12))
        self.assertEqual((commit, reason), ("b", None))
        self.assertIsNone(fh.pick_seed([("a", at(2026, 11, 11, 7), None)], date(2026, 11, 11)))  # no forecast file yet
        stale = [("d", at(2026, 11, 12, 9, 30), good)]  # fetched 3:10 AM, 6h20m before
        self.assertIn("more than 6 hours old", fh.pick_seed(stale, date(2026, 11, 12))[3])


if __name__ == "__main__":
    unittest.main()
