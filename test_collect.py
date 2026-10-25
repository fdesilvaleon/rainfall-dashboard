"""Offline tests for collect.py. Run with: python -m unittest"""
import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import collect


def page(*rows):
    """Build a fake report page from (time, value) pairs."""
    return "".join(f"<tr><td id=DateandTime>{t}</td><td id=Rainfall>{v}</td></tr>" for t, v in rows)


def read(path):
    with open(path, newline="") as f:
        return [list(r.values()) for r in csv.DictReader(f)]


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "x.csv"

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, *rows):
        return collect.merge(self.path, collect.to_hourly(collect.parse(page(*rows))))

    def test_keeps_last_reading_of_each_hour_and_computes_rain(self):
        self.save(("10/25/2026 07:55:00 AM", "0.20"),
                  ("10/25/2026 08:05:00 AM", "0.24"),
                  ("10/25/2026 08:55:00 AM", "0.31"))
        self.assertEqual(read(self.path), [["2026-10-25 07:00", "0.20", ""],
                                           ["2026-10-25 08:00", "0.31", "0.11"]])

    def test_missing_readings(self):
        # "----" at the end of an hour: use the last real reading in that hour.
        # An hour with only "----": total and rain blank, and the next hour's rain blank too.
        self.save(("10/25/2026 07:50:00 AM", "0.20"), ("10/25/2026 07:55:00 AM", "----"),
                  ("10/25/2026 08:55:00 AM", "----"),
                  ("10/25/2026 09:55:00 AM", "0.25"))
        self.assertEqual(read(self.path), [["2026-10-25 07:00", "0.20", ""],
                                           ["2026-10-25 08:00", "", ""],
                                           ["2026-10-25 09:00", "0.25", ""]])

    def test_negative_difference_is_blank(self):
        self.save(("10/01/2026 12:55:00 AM", "5.00"), ("10/01/2026 01:55:00 AM", "0.00"))
        self.assertEqual(read(self.path)[1], ["2026-10-01 01:00", "0.00", ""])

    def test_gap_between_hours_is_blank(self):
        self.save(("10/25/2026 06:55:00 AM", "0.10"), ("10/25/2026 08:55:00 AM", "0.30"))
        self.assertEqual(read(self.path)[1], ["2026-10-25 08:00", "0.30", ""])

    def test_merge_without_duplicates_and_current_hour_updates(self):
        self.save(("10/25/2026 07:55:00 AM", "0.20"), ("10/25/2026 08:15:00 AM", "0.22"))
        self.assertEqual(self.save(("10/25/2026 07:55:00 AM", "0.20"), ("10/25/2026 08:15:00 AM", "0.22")), 0)
        # An hour later the page has the rest of 08:00 and some of 09:00, but no longer 07:00.
        self.save(("10/25/2026 08:55:00 AM", "0.30"), ("10/25/2026 09:10:00 AM", "0.30"))
        self.assertEqual(read(self.path), [["2026-10-25 07:00", "0.20", ""],
                                           ["2026-10-25 08:00", "0.30", "0.10"],
                                           ["2026-10-25 09:00", "0.30", "0.00"]])

    def test_blank_never_replaces_a_number(self):
        self.save(("10/25/2026 08:55:00 AM", "0.30"))
        self.save(("10/25/2026 08:55:00 AM", "----"))
        self.assertEqual(read(self.path), [["2026-10-25 08:00", "0.30", ""]])

    def test_one_failing_gauge_does_not_stop_the_others(self):
        gauges = [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]

        def fake_fetch(gid):
            if gid == "1":
                raise OSError("simulated outage")
            return page(("10/25/2026 08:55:00 AM", "0.30"))

        with mock.patch.object(collect, "DATA_DIR", Path(self.tmp.name)), \
             mock.patch.object(collect, "load_gauges", return_value=gauges), \
             mock.patch.object(collect, "fetch", fake_fetch), \
             mock.patch.object(collect, "PAUSE_SECONDS", 0):
            collect.main()
        self.assertFalse((Path(self.tmp.name) / "1.csv").exists())
        self.assertTrue((Path(self.tmp.name) / "2.csv").exists())


if __name__ == "__main__":
    unittest.main()
