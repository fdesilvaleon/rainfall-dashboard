"""Offline tests for the forecast history. Run with: python -m unittest

No test talks to the National Weather Service or to git.
"""
import csv
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import forecast_history as fh
import rainfall as rf

LOCAL = fh.LOCAL


def day(d, rain, chance, complete=True):
    return {"date": d, "rain_in": rain, "complete": complete, "hours_covered": 24, "chance_max_pct": chance, "periods": []}


WEEK = [day("2026-11-12", 0.0, 5, complete=False), day("2026-11-13", 0.52, 98), day("2026-11-14", None, None),
        day("2026-11-15", 0.23, 87), day("2026-11-16", 0.39, 87), day("2026-11-17", 0, 32), day("2026-11-18", 0.49, 73)]


def fc(issued="2026-11-12T10:26:47+00:00", days=None, error=None, issued_6140=None):
    def gauge(t):
        return {"name": "G", "grid": {}, "nws_updated": t, "fetched_at": "2026-11-12T07:00:00-08:00", "days": days or WEEK}
    return {"gauges": {"6121": gauge(issued), "6140": gauge(issued_6140 or issued)}, "last_error": error}


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

    def rows(self, path=None):
        with open(path or fh.history_file(), newline="") as f:
            return list(csv.DictReader(f))

    def test_new_issue_time_saved_with_lead_days_0_to_3_only(self):
        result = fh.save_snapshot(fc(), at(2026, 11, 12, 7), self.log)
        self.assertEqual(result, "saved 8 rows (issue time 2026-11-12T10:26:47Z)")
        r = [x for x in self.rows() if x["gauge_id"] == "6121"]
        self.assertEqual([x["target_date"] for x in r], ["2026-11-12", "2026-11-13", "2026-11-14", "2026-11-15"])
        self.assertEqual([x["lead_days"] for x in r], ["0", "1", "2", "3"])
        self.assertEqual(r[0], {"snapshot_time": "2026-11-12T10:26:47Z", "saved_at": "2026-11-12T15:00:00Z", "gauge_id": "6121",
                                "target_date": "2026-11-12", "lead_days": "0", "expected_in": "0", "chance_pct": "5", "partial_day": "1"})
        self.assertEqual((r[2]["expected_in"], r[2]["chance_pct"]), ("", ""))  # unknown stays blank, not 0

    def test_repeated_issue_time_not_saved_twice(self):
        fh.save_snapshot(fc(), at(2026, 11, 12, 7), self.log)
        self.assertEqual(fh.save_snapshot(fc(), at(2026, 11, 12, 8), self.log), "no new issue time")
        self.assertEqual(len(self.rows()), 8)
        # Only the gauge whose grid cell has a new issue time gets new rows.
        fh.save_snapshot(fc(issued_6140="2026-11-12T18:49:07+00:00"), at(2026, 11, 12, 11), self.log)
        self.assertEqual(len(self.rows()), 12)
        self.assertEqual({r["gauge_id"] for r in self.rows()[8:]}, {"6140"})

    def test_lead_days_across_midnight_and_daylight_saving(self):
        target = [day("2026-11-12", 0.1, 10), day("2026-11-13", 0.2, 20)]
        # Issued 11:30 PM Pacific on Nov 11 (07:30 UTC Nov 12): Nov 12 is 1 day ahead.
        rows = fh.rows_for(fc("2026-11-12T07:30:00+00:00", target), at(2026, 11, 12, 7))
        self.assertEqual([r["lead_days"] for r in rows[:2]], [1, 2])
        # Issued 12:30 AM Pacific on Nov 12: same day.
        rows = fh.rows_for(fc("2026-11-12T08:30:00+00:00", target), at(2026, 11, 12, 7))
        self.assertEqual([r["lead_days"] for r in rows[:2]], [0, 1])
        # Summer time: 07:30 UTC Jul 1 is 12:30 AM PDT Jul 1 (it would still be Jun 30 in PST).
        rows = fh.rows_for(fc("2026-07-01T07:30:00+00:00", [day("2026-07-01", 0, 0)]), at(2026, 7, 1, 7))
        self.assertEqual(rows[0]["lead_days"], 0)
        # The night clocks go back: 07:30 UTC Nov 1 2026 is 12:30 AM PDT Nov 1.
        rows = fh.rows_for(fc("2026-11-01T07:30:00+00:00", [day("2026-11-01", 0, 0)]), at(2026, 11, 1, 7))
        self.assertEqual(rows[0]["lead_days"], 0)

    def test_missing_failed_or_errored_forecast_saves_nothing(self):
        for bad, why in [(None, "no forecast"), ({"gauges": {}}, "no forecast"),
                         (fc(error={"time": "x", "message": "HTTP Error 500"}), "HTTP Error 500")]:
            self.assertIn(why, fh.save_snapshot(bad, at(2026, 11, 12, 7), self.log))
        self.assertFalse(fh.history_file().exists())

    def test_old_rows_are_never_changed(self):
        fh.save_snapshot(fc(), at(2026, 11, 12, 7), self.log)
        before = fh.history_file().read_bytes()
        fh.save_snapshot(fc("2026-11-12T18:49:07+00:00"), at(2026, 11, 12, 11), self.log)
        after = fh.history_file().read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertGreater(len(after), len(before))

    def test_backfill_order_and_duplicates(self):
        fh.append(fh.rows_for(fc("2026-11-12T10:26:47+00:00"), at(2026, 11, 12, 7)))  # already in the history
        commits = [
            ("a", at(2026, 11, 12, 3, 10), fc("2026-11-12T10:26:47+00:00")),           # already saved: skipped
            ("b", at(2026, 11, 12, 11, 0), fc("2026-11-12T18:49:07+00:00", issued_6140="2026-11-12T08:26:36+00:00")),
            ("c", at(2026, 11, 12, 12, 0), fc("2026-11-12T18:49:07+00:00")),           # 6121's issue time seen in "b"
            ("d", at(2026, 11, 12, 13, 0), fc("2026-11-12T20:26:29+00:00", error={"message": "boom"})),
        ]
        rows, skipped = fh.backfill_rows(commits, fh.saved_keys())
        keys = []
        for r in rows:
            if (r["snapshot_time"], r["gauge_id"]) not in keys:
                keys.append((r["snapshot_time"], r["gauge_id"]))
        # In order of issue time; each (gauge, issue time) once, from the first commit it appears in.
        self.assertEqual(keys, [("2026-11-12T08:26:36Z", "6140"), ("2026-11-12T18:49:07Z", "6121"), ("2026-11-12T18:49:07Z", "6140")])
        self.assertEqual({r["saved_at"] for r in rows if r["gauge_id"] == "6121"}, {"2026-11-12T19:00:00Z"})  # from "b"
        self.assertEqual(skipped, [("d", "the forecast fetch had a problem (boom)")])

    def test_recent_file_keeps_last_21_days_sorted(self):
        old = [day("2026-10-01", 0.3, 50)]
        fh.append(fh.rows_for(fc("2026-10-01T15:00:00+00:00", old), at(2026, 10, 1, 8)))
        fh.save_snapshot(fc("2026-11-12T18:49:07+00:00"), at(2026, 11, 12, 11), self.log)
        fh.save_snapshot(fc("2026-11-12T10:26:47+00:00"), at(2026, 11, 12, 12), self.log)
        recent = self.rows(fh.recent_file())
        self.assertNotIn("2026-10-01", {r["target_date"] for r in recent})
        self.assertEqual(len(recent), 16)
        order = [(r["target_date"], r["snapshot_time"]) for r in recent]
        self.assertEqual(order, sorted(order))
        self.assertEqual(len(self.rows()), 18)  # the full history keeps everything
        # Exactly 21 days back is kept; 22 isn't.
        fh.append(fh.rows_for(fc("2026-10-22T15:00:00+00:00", [day("2026-10-22", 0, 0)]), at(2026, 10, 22, 8)))
        fh.append(fh.rows_for(fc("2026-10-21T15:00:00+00:00", [day("2026-10-21", 0, 0)]), at(2026, 10, 21, 8)))
        fh.write_recent(date(2026, 11, 12), self.log)
        dates = {r["target_date"] for r in self.rows(fh.recent_file())}
        self.assertIn("2026-10-22", dates)
        self.assertNotIn("2026-10-21", dates)

    def test_recent_file_problem_is_logged_not_raised(self):
        messages = []
        with mock.patch.object(fh, "read_rows", side_effect=OSError("disk full")):
            self.assertIsNone(fh.write_recent(date(2026, 11, 12), messages.append))
        self.assertIn("disk full", messages[0])


if __name__ == "__main__":
    unittest.main()
