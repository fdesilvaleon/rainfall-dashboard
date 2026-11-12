"""Forecast history: keep a copy of every National Weather Service forecast we see, so past
forecasts can later be compared with the rain the gauges measured.

data/forecast_history.csv: rows are only ever added at the end; old rows are never changed or
deleted. One row per gauge, per target day, per NWS issue time:

  snapshot_time  when NWS issued or last updated that forecast (UTC)
  saved_at       when we saved it (UTC)
  gauge_id
  target_date    the day the forecast is for (Pacific local date)
  lead_days      target_date minus the Pacific local date of snapshot_time (0 = same day)
  expected_in    expected rain that day, in inches (blank if NWS gave no amount)
  chance_pct     highest chance of rain that day, in % (blank if unknown)
  partial_day    1 if the forecast covers only part of that day, otherwise 0

When: every collector run checks the forecast it has just fetched. A gauge's forecast is saved
if its NWS issue time isn't in the history yet for that gauge, so the same issue time is never
saved twice. Only target days 0 to 3 days ahead are kept (up to 4 rows per gauge, 28 per issue
time). A missing forecast, a failed fetch or one with a recorded error saves nothing.

data/forecast_history_recent.csv: the rows whose target day is 21 days ago or later, sorted by
target_date then snapshot_time. Rebuilt from the full history every run; never edit it.

One-time backfill from the git history of data/forecast.json (run by hand, not on GitHub):
    python forecast_history.py --backfill-from-git
"""
import csv
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

import forecast
import rainfall as rf

LOCAL = forecast.LOCAL
LEAD_DAYS = range(0, 4)  # keep forecasts for the same day up to 3 days ahead
RECENT_DAYS = 21
FIELDS = ["snapshot_time", "saved_at", "gauge_id", "target_date", "lead_days", "expected_in", "chance_pct", "partial_day"]


def history_file():
    return rf.DATA_DIR / "forecast_history.csv"


def recent_file():
    return rf.DATA_DIR / "forecast_history_recent.csv"


def utc_text(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_rows():
    path = history_file()
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def saved_keys():
    """(gauge_id, snapshot_time) pairs already in the history."""
    return {(r["gauge_id"], r["snapshot_time"]) for r in read_rows()}


def problem(fc):
    """Why nothing should be saved from this forecast, or None if it's fine."""
    if not fc or not fc.get("gauges"):
        return "no forecast available"
    if fc.get("last_error"):
        return f"the forecast fetch had a problem ({fc['last_error'].get('message')})"
    return None


def rows_for(fc, saved_at, skip=frozenset()):
    """History rows (lead days 0 to 3 only) for each gauge whose issue time isn't in `skip`."""
    rows = []
    for gid, g in fc["gauges"].items():
        if not g.get("nws_updated"):
            continue  # no issue time: nothing to label the rows with
        snapshot = datetime.fromisoformat(g["nws_updated"].replace("Z", "+00:00"))
        key = (gid, utc_text(snapshot))
        if key in skip:
            continue
        snapshot_day = snapshot.astimezone(LOCAL).date()
        for d in g.get("days", []):
            lead = (date.fromisoformat(d["date"]) - snapshot_day).days
            if lead not in LEAD_DAYS:
                continue
            chance = d.get("chance_max_pct")
            rows.append({
                "snapshot_time": key[1],
                "saved_at": utc_text(saved_at),
                "gauge_id": gid,
                "target_date": d["date"],
                "lead_days": lead,
                "expected_in": rf.fmt_rain(d.get("rain_in")),  # blank when NWS gave no amount
                "chance_pct": "" if chance is None else (int(chance) if float(chance).is_integer() else chance),
                "partial_day": 0 if d.get("complete") else 1,
            })
    return rows


def append(rows):
    """Add rows at the end of the history (creating it with a header if needed). Never rewrites old rows."""
    path = history_file()
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="") as f:
        out = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            out.writeheader()
        out.writerows(rows)


def write_recent(today, log=print):
    """Rebuild the recent file from the full history. Problems are logged, never raised."""
    try:
        first = (today - timedelta(days=RECENT_DAYS)).isoformat()
        rows = [r for r in read_rows() if r["target_date"] >= first]
        rows.sort(key=lambda r: (r["target_date"], r["snapshot_time"], r["gauge_id"]))
        path = recent_file()
        temp = path.with_suffix(".tmp")
        with open(temp, "w", newline="") as f:
            out = csv.DictWriter(f, fieldnames=FIELDS)
            out.writeheader()
            out.writerows(rows)
        if path.exists() and path.read_bytes() == temp.read_bytes():
            temp.unlink()  # unchanged: leave the file alone so nothing new gets committed
        else:
            os.replace(temp, path)
        return len(rows)
    except Exception as e:
        log(f"Forecast history: the recent file wasn't updated ({e})")
        return None


def save_snapshot(fc, now=None, log=print):
    """Called after each collector run with the forecast it just fetched. Returns what happened, in words."""
    now = now or forecast.now_local()
    reason = problem(fc)
    if reason:
        result = f"nothing saved: {reason}"
    else:
        rows = rows_for(fc, now, skip=saved_keys())
        if rows:
            append(rows)
            issued = sorted({r["snapshot_time"] for r in rows})
            result = f"saved {len(rows)} rows (issue time {', '.join(issued)})"
        else:
            result = "no new issue time"
    log(f"Forecast history: {result}")
    write_recent(now.astimezone(LOCAL).date(), log)
    return result


# ---------- one-time backfill from git ----------

def git(*args):
    return subprocess.run(["git", *args], cwd=rf.ROOT, capture_output=True, text=True, check=True).stdout


def backfill_rows(commits, have):
    """From [(commit, time, forecast or None)] in time order: rows for every (gauge, issue time) not in `have`,
    taken from the first commit where it appears, sorted by issue time. Also returns the skipped commits."""
    found, skipped = {}, []
    for commit, when, fc in commits:
        reason = problem(fc)
        if reason:
            skipped.append((commit, reason))
            continue
        # Issue times already in the history, or found in an earlier commit, are skipped,
        # so each one is taken from the first commit where it appears.
        for row in rows_for(fc, when, skip=have | set(found)):
            found.setdefault((row["gauge_id"], row["snapshot_time"]), []).append(row)
    rows = [r for key in sorted(found, key=lambda k: (k[1], k[0])) for r in found[key]]
    return rows, skipped


def backfill_from_git(log=print):
    have = saved_keys()
    commits = []
    for line in git("log", "--reverse", "--format=%H %cI", "--", "data/forecast.json").split("\n"):
        if line.strip():
            commit, when = line.split()
            commits.append((commit, datetime.fromisoformat(when), json.loads(git("show", f"{commit}:data/forecast.json"))))
    rows, skipped = backfill_rows(commits, have)
    append(rows)
    issued = sorted({r["snapshot_time"] for r in rows})
    log(f"Looked at {len(commits)} commits of data/forecast.json; added {len(rows)} rows for {len(issued)} new issue times"
        + (f" ({issued[0]} to {issued[-1]})" if issued else ""))
    for commit, reason in skipped:
        log(f"Skipped commit {commit[:7]}: {reason}")
    write_recent(forecast.now_local().date(), log)


if __name__ == "__main__":
    if sys.argv[1:] == ["--backfill-from-git"]:
        backfill_from_git()
    else:
        print(__doc__)
