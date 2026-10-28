"""Hourly collector, run by GitHub Actions.

For each gauge in gauges.csv it asks the district's API for the last 48 hours of
rain and updates:
  data/<id>.csv                hourly rain for the current and previous water year
  data/history/<id>_daily.csv  daily rain history
  data/stats.json              statistics for the website (recomputed every run)

Polite access: one request per gauge, at least 5 seconds apart. On an error or
timeout a request is tried again at most twice (after 30 s, then 2 min). If it
still fails, that gauge is skipped until the next run, which catches up.
"""
import sys
from datetime import datetime, time as dtime, timedelta

import rainfall as rf

GAP_SECONDS = 5
WINDOW_HOURS = 48
GAP_LOOKBACK_DAYS = 30  # hours missing this recently are filled in (by starting the one request earlier)


def first_missing_hour(rain, first, last):
    for h in rf.hours_between(first, last):
        if rain.get(h) is None:
            return h
    return None


def update_gauge(gauge, now, fetch):
    """Fetch recent rain for one gauge and update its files. Returns (number of rainy values, first day fetched)."""
    gid = gauge["id"]
    hourly_path = rf.DATA_DIR / f"{gid}.csv"
    daily_path = rf.HISTORY_DIR / f"{gid}_daily.csv"
    current_hour = rf.hour_of(now)
    keep_from = datetime.combine(rf.wy_start(rf.water_year(now) - 1), dtime())

    rain = rf.read_hourly(hourly_path)
    start_day = (now - timedelta(hours=WINDOW_HOURS)).date()
    look_from = max(keep_from, datetime.combine((now - timedelta(days=GAP_LOOKBACK_DAYS)).date(), dtime()))
    gap = first_missing_hour(rain, look_from, current_hour)
    if gap is not None and gap.date() < start_day:
        start_day = gap.date()

    readings = fetch(gid, start_day, now.date())
    # Hours in the fetched period are replaced, so late or corrected readings are picked up.
    rain.update(rf.hourly_totals(readings, datetime.combine(start_day, dtime()), current_hour))

    daily = rf.read_daily(daily_path)
    for d in rf.days_between(start_day, now.date()):
        daily[d] = rf.day_total(rain, d)
    rf.write_daily(daily_path, daily, min(daily), now.date())

    rf.DATA_DIR.mkdir(exist_ok=True)
    rain = {h: v for h, v in rain.items() if keep_from <= h <= current_hour}
    rf.write_hourly(hourly_path, rain, keep_from, current_hour)
    return len(readings), start_day


def main():
    gauges = rf.load_gauges()
    now = rf.now_pst()
    pacer = rf.Pacer(GAP_SECONDS)
    failures = 0
    for g in gauges:
        try:
            n, start = update_gauge(g, now, lambda gid, a, b: rf.fetch_rain(gid, a, b, pacer))
            print(f"{g['id']} {g['name']}: {n} rainy 15-minute periods since {start}")
        except Exception as e:
            failures += 1
            print(f"{g['id']} {g['name']}: SKIPPED this run ({e})", file=sys.stderr)
    try:
        rf.write_stats(gauges, now.date())
    except Exception as e:
        print(f"Statistics not updated ({e})", file=sys.stderr)
    # Only report failure if every gauge failed, so one bad gauge doesn't stop the others being saved.
    if failures == len(gauges):
        sys.exit(1)


if __name__ == "__main__":
    main()
