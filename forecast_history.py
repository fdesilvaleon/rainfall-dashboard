"""Forecast history: once a day, keep a copy of the National Weather Service forecast,
so past forecasts can later be compared with the rain the gauges measured.

Appends rows to data/forecast_history.csv. Old rows are never changed or deleted.
One row per gauge, per day forecast (target day), per daily snapshot:

  snapshot_time  when NWS issued or last updated that forecast (UTC)
  saved_at       when we saved it (UTC)
  gauge_id
  target_date    the day the forecast is for (Pacific local date)
  lead_days      target_date minus the Pacific local date of snapshot_time (0 = same day)
  expected_in    expected rain that day, in inches (blank if NWS gave no amount)
  chance_pct     highest chance of rain that day, in % (blank if unknown)
  partial_day    1 if the forecast covers only part of that day, otherwise 0

When: the first collector run at or after 6 AM Pacific time each day, using the forecast that
run has just fetched. At most one snapshot per Pacific date. If the forecast is missing, had a
problem or is stale, nothing is saved in that run (the next run that day tries again).

One-time seeding from the git history of data/forecast.json (run by hand, not on GitHub):
    python forecast_history.py --seed-from-git
"""
import csv
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import forecast
import rainfall as rf

LOCAL = forecast.LOCAL
SAVE_FROM_HOUR = 6   # Pacific local time
STALE_HOURS = 6      # the same rule the dashboard uses for an out-of-date forecast
FIELDS = ["snapshot_time", "saved_at", "gauge_id", "target_date", "lead_days", "expected_in", "chance_pct", "partial_day"]


def history_file():
    return rf.DATA_DIR / "forecast_history.csv"


def utc_text(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def saved_dates():
    """Pacific dates that already have a snapshot (from the saved_at column)."""
    path = history_file()
    if not path.exists():
        return set()
    with open(path, newline="") as f:
        return {datetime.fromisoformat(r["saved_at"].replace("Z", "+00:00")).astimezone(LOCAL).date()
                for r in csv.DictReader(f) if r.get("saved_at")}


def problem(fc, now):
    """Why this forecast shouldn't be saved as a snapshot at `now`, or None if it's fine."""
    if not fc or not fc.get("gauges"):
        return "no forecast available"
    if fc.get("last_error"):
        return f"the forecast fetch had a problem ({fc['last_error'].get('message')})"
    for gid, g in fc["gauges"].items():
        fetched = g.get("fetched_at")
        if not fetched or now - datetime.fromisoformat(fetched) > timedelta(hours=STALE_HOURS):
            return f"the forecast for gauge {gid} is more than {STALE_HOURS} hours old"
        if not g.get("nws_updated"):
            return f"the forecast for gauge {gid} has no NWS update time"
    return None


def rows_for(fc, saved_at):
    """History rows for every gauge and every target day in a forecast."""
    rows = []
    for gid, g in fc["gauges"].items():
        snapshot = datetime.fromisoformat(g["nws_updated"].replace("Z", "+00:00"))
        snapshot_day = snapshot.astimezone(LOCAL).date()
        for d in g.get("days", []):
            target = date.fromisoformat(d["date"])
            chance = d.get("chance_max_pct")
            rows.append({
                "snapshot_time": utc_text(snapshot),
                "saved_at": utc_text(saved_at),
                "gauge_id": gid,
                "target_date": d["date"],
                "lead_days": (target - snapshot_day).days,
                "expected_in": rf.fmt_rain(d.get("rain_in")),  # blank when NWS gave no amount
                "chance_pct": "" if chance is None else (int(chance) if float(chance).is_integer() else chance),
                "partial_day": 0 if d.get("complete") else 1,
            })
    return rows


def append(rows):
    """Add rows at the end of the file (creating it with a header if needed). Never rewrites old rows."""
    path = history_file()
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="") as f:
        out = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            out.writeheader()
        out.writerows(rows)


def save_daily_snapshot(fc, now=None, log=print):
    """Called after each collector run with the forecast it just fetched. Returns what happened, in words."""
    now = now or forecast.now_local()
    local = now.astimezone(LOCAL)
    if local.hour < SAVE_FROM_HOUR:
        result = f"not yet ({SAVE_FROM_HOUR} AM Pacific or later)"
    elif local.date() in saved_dates():
        result = f"already saved for {local.date()}"
    else:
        reason = problem(fc, now)
        if reason:
            result = f"skipped for now: {reason}; the next run will try again"
        else:
            rows = rows_for(fc, now)
            append(rows)
            result = f"saved {len(rows)} rows for {local.date()}"
    log(f"Forecast history: {result}")
    return result


# ---------- one-time seeding from git ----------

def git(*args):
    return subprocess.run(["git", *args], cwd=rf.ROOT, capture_output=True, text=True, check=True).stdout


def pick_seed(commits, day):
    """From [(commit, time, forecast or None)] in time order, the snapshot for one Pacific day:
    the forecast as it stood after the first run at or after 6 AM that day, if it's usable."""
    start = datetime(day.year, day.month, day.day, SAVE_FROM_HOUR, tzinfo=LOCAL)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=LOCAL)
    for commit, when, fc in commits:
        if start <= when < end and fc is not None:
            reason = problem(fc, when)
            return (commit, when, fc, reason)
    return None


def seed_from_git(today=None, log=print):
    today = today or forecast.now_local().date()
    have = saved_dates()
    commits = []
    for line in git("log", "--reverse", "--format=%H %cI", "--", "data").split("\n"):
        if not line.strip():
            continue
        commit, when = line.split()
        when = datetime.fromisoformat(when)
        try:
            fc = json.loads(git("show", f"{commit}:data/forecast.json"))
        except subprocess.CalledProcessError:
            fc = None  # the forecast file didn't exist yet
        commits.append((commit, when, fc))
    for day in (today - timedelta(days=1), today):
        if day in have:
            log(f"{day}: already in the history, not seeded")
            continue
        found = pick_seed(commits, day)
        if found is None:
            log(f"{day}: no forecast snapshot from 6 AM onward in git history, not seeded")
            continue
        commit, when, fc, reason = found
        if reason:
            log(f"{day}: snapshot {commit[:7]} not usable ({reason}), not seeded")
            continue
        rows = rows_for(fc, when)
        append(rows)
        issued = sorted({g["nws_updated"] for g in fc["gauges"].values()})
        log(f"{day}: seeded {len(rows)} rows from commit {commit[:7]} (saved {when.astimezone(LOCAL):%Y-%m-%d %H:%M %Z}), "
            f"NWS issue time(s): {', '.join(issued)}")


if __name__ == "__main__":
    if sys.argv[1:] == ["--seed-from-git"]:
        seed_from_git()
    else:
        print(__doc__)
