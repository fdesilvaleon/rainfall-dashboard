"""OLD COLLECTOR, KEPT FOR REFERENCE ONLY. It is not scheduled and should not be run.

It scraped the district's report pages (valleywateralert.org) and was replaced by
collect.py, which uses the district's API. Run from this folder it would not find
gauges.csv; run from the main folder it would overwrite data/<id>.csv in the old format.

Download rain gauge readings from valleywateralert.org and save hourly data to data/<id>.csv.

The district's pages list 5-minute readings of the cumulative season total (inches).
We keep one row per hour: the last reading in that hour, plus rain_in_hour, which is
this hour's season total minus the previous hour's. Times are Pacific Standard Time,
as shown on the district's site, and each hour is labeled by its start (08:00 = 08:00-08:59).
"""
import csv
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).parent
GAUGES_FILE = ROOT / "gauges.csv"
DATA_DIR = ROOT / "data"
REPORT_URL = "https://valleywateralert.org/scvwd/reports/pgi_report.php?id={id}"
USER_AGENT = "mountain-view-rainfall-dashboard (personal, hourly; GitHub Pages project)"
PAUSE_SECONDS = 2  # wait between gauges so we don't hit the server all at once

ROW_RE = re.compile(r"<tr><td[^>]*>(.*?)</td><td[^>]*>(.*?)</td></tr>")
NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
TIME_FORMAT = "%Y-%m-%d %H:%M"
FIELDS = ["hour_pst", "season_total_in", "rain_in_hour"]


def load_gauges():
    with open(GAUGES_FILE, newline="") as f:
        return [row for row in csv.DictReader(f) if row["id"].strip()]


def fetch(gauge_id):
    req = urllib.request.Request(REPORT_URL.format(id=gauge_id), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("latin-1")


def parse(html):
    """Return {datetime: value} of 5-minute readings from a report page. Value is '' when missing ("----")."""
    readings = {}
    for raw_time, raw_value in ROW_RE.findall(html):
        try:
            t = datetime.strptime(raw_time.strip(), "%m/%d/%Y %I:%M:%S %p")
        except ValueError:
            continue  # not a data row
        value = raw_value.strip()
        readings[t] = value if NUMBER_RE.match(value) else ""
    return readings


def to_hourly(readings):
    """Return {hour label: season total} using the last non-missing reading in each hour ('' if none)."""
    hourly = {}
    for t in sorted(readings):
        label = t.replace(minute=0, second=0).strftime(TIME_FORMAT)
        if readings[t] != "" or label not in hourly:
            hourly[label] = readings[t]
    return hourly


def add_rain_in_hour(totals):
    """Return rows with rain_in_hour = this hour's total minus the previous hour's.

    Blank if either value is missing, the previous hour isn't in the file, or the
    difference is negative (for example when the season total resets).
    """
    rows = []
    for label in sorted(totals):
        previous = (datetime.strptime(label, TIME_FORMAT) - timedelta(hours=1)).strftime(TIME_FORMAT)
        now, before = totals[label], totals.get(previous, "")
        rain = ""
        if now != "" and before != "":
            diff = Decimal(now) - Decimal(before)
            if diff >= 0:
                rain = str(diff)
        rows.append([label, now, rain])
    return rows


def merge(path, new_hourly):
    """Merge new hourly totals into the gauge's file. Returns how many hours were added or changed."""
    totals = {}
    if path.exists():
        with open(path, newline="") as f:
            totals = {row["hour_pst"]: row["season_total_in"] for row in csv.DictReader(f)}
    changed = 0
    for label, value in new_hourly.items():
        old = totals.get(label)
        # New hours are added. Hours we already have are updated with the latest page
        # (the current hour fills in as the hour goes on), but a blank never replaces a number.
        if old is None or (value != "" and value != old):
            totals[label] = value
            changed += 1
    if changed or not path.exists():
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(FIELDS)
            writer.writerows(add_rain_in_hour(totals))
    return changed


def main():
    DATA_DIR.mkdir(exist_ok=True)
    gauges = load_gauges()
    failures = 0
    for i, gauge in enumerate(gauges):
        if i:
            time.sleep(PAUSE_SECONDS)
        gid, name = gauge["id"].strip(), gauge["name"].strip()
        try:
            readings = parse(fetch(gid))
            if not readings:
                raise ValueError("no readings found on the page")
            hourly = to_hourly(readings)
            changed = merge(DATA_DIR / f"{gid}.csv", hourly)
            print(f"{gid} {name}: {len(readings)} readings on page ({len(hourly)} hours), {changed} hours new or updated")
        except Exception as e:
            failures += 1
            print(f"{gid} {name}: FAILED ({e})", file=sys.stderr)
    # Only report failure if every gauge failed, so one bad gauge doesn't stop the others being saved.
    if failures == len(gauges):
        sys.exit(1)


if __name__ == "__main__":
    main()
