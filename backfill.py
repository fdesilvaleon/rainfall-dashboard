"""One-time history download. Run it by hand, not from GitHub Actions:

    python backfill.py               # every gauge in gauges.csv
    python backfill.py 6121 6140     # only these gauges (for example a newly added one)

For each gauge it makes ONE request covering the last 15 full water years up to
today, at least 10 seconds after the previous request, and then writes:
  data/history/<id>_daily.csv  daily rain from Oct 1, 15 water years ago, to today
  data/<id>.csv                hourly rain for the current and previous water year
and recomputes data/stats.json. Existing files for those gauges are replaced.

Days before a gauge's first recorded rain are left blank (no data).

--raw-dir DIR saves each API response in DIR, and reuses a saved response
instead of asking the API again.
"""
import argparse
import json
import sys
from datetime import datetime, time as dtime
from pathlib import Path

import rainfall as rf

GAP_SECONDS = 10


def get_history(gid, start, end, pacer, raw_dir=None):
    """Return (readings, time they were fetched)."""
    saved = Path(raw_dir) / f"{gid}.json" if raw_dir else None
    if saved and saved.exists():
        fetched_at = datetime.fromtimestamp(saved.stat().st_mtime, rf.PST).replace(tzinfo=None)
        return rf.parse_response(json.loads(saved.read_text()), gid), fetched_at
    fetched_at = rf.now_pst()
    keep = (lambda payload: saved.write_text(json.dumps(payload))) if saved else None
    return rf.fetch_rain(gid, start, end, pacer, on_payload=keep), fetched_at


def build_gauge(gid, readings, now):
    current_hour = rf.hour_of(now)
    first_day = rf.wy_start(rf.water_year(now) - rf.HISTORY_YEARS)
    keep_from = datetime.combine(rf.wy_start(rf.water_year(now) - 1), dtime())
    rainy = [t for t, v in readings if v > 0]
    first_data = rf.hour_of(min(rainy)) if rainy else None

    hourly = rf.hourly_totals(readings, datetime.combine(first_day, dtime()), current_hour)
    for h in hourly:
        if first_data is None or h < first_data.replace(hour=0):
            hourly[h] = None  # before the gauge's records start
    daily = {d: rf.day_total(hourly, d) for d in rf.days_between(first_day, now.date())}

    rf.write_daily(rf.HISTORY_DIR / f"{gid}_daily.csv", daily, first_day, now.date())
    rf.DATA_DIR.mkdir(exist_ok=True)
    rf.write_hourly(rf.DATA_DIR / f"{gid}.csv", {h: v for h, v in hourly.items() if h >= keep_from}, keep_from, current_hour)
    return first_data


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*", help="gauge ids (default: all in gauges.csv)")
    parser.add_argument("--raw-dir", help="folder to save API responses in, and reuse them from")
    args = parser.parse_args()

    gauges = rf.load_gauges()
    chosen = [g for g in gauges if not args.ids or g["id"] in args.ids]
    if args.raw_dir:
        Path(args.raw_dir).mkdir(parents=True, exist_ok=True)
    pacer = rf.Pacer(GAP_SECONDS)
    today = rf.now_pst().date()
    start = rf.wy_start(rf.water_year(today) - rf.HISTORY_YEARS)
    for g in chosen:
        try:
            readings, fetched_at = get_history(g["id"], start, today, pacer, args.raw_dir)
            first = build_gauge(g["id"], readings, fetched_at)
            print(f"{g['id']} {g['name']}: {len(readings)} rainy 15-minute periods; first in this period: {first:%Y-%m-%d}" if first
                  else f"{g['id']} {g['name']}: no rain recorded in this period")
        except Exception as e:
            print(f"{g['id']} {g['name']}: FAILED ({e})", file=sys.stderr)
    rf.write_stats(gauges, today)
    print("Wrote", rf.STATS_FILE)


if __name__ == "__main__":
    main()
