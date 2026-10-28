"""Shared code for the rainfall collector.

Data comes from Santa Clara Valley Water's Surface Water Data API
(https://alertdata.valleywater.org). For each gauge the API gives the rain in
each 15-minute period. A value stamped 02:00 is the rain from 01:45 to 02:00,
and all times are Pacific Standard Time (PST) all year, as the district reports
them.

The API reports 0 both when it didn't rain and when a gauge wasn't reporting,
so we only ask for the non-zero values (skipZeroValues=true). That is all the
information there is, in a much smaller response.

Water years run from Oct 1 to Sep 30 and are named by both years, e.g. "2025-26".
"""
import csv
import json
import re
import statistics
import time
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
GAUGES_FILE = ROOT / "gauges.csv"
DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
STATS_FILE = DATA_DIR / "stats.json"

API = "https://alertdata.valleywater.org"
USER_AGENT = "rainfall-dashboard personal project (github.com/fdesilvaleon/rainfall-dashboard)"
TIMEOUT_SECONDS = 60
RETRY_WAITS = (30, 120)  # after a failed call, wait 30 s and try again, then 2 min and try once more

PST = timezone(timedelta(hours=-8))
HISTORY_YEARS = 15        # full water years kept in the daily history and used for statistics
COMPLETE_SHARE = 0.95     # a water year counts as complete if at least 95% of its days have data
LIMITED_YEARS = 5         # fewer complete years than this is flagged as "limited history"
OUTAGE_DAY_IN = 0.5       # see suspected_outages()
OUTAGE_RUN_IN = 1.0

# Water years left out of the statistics by hand, per gauge: {gauge id: {water year: reason}}.
# The daily data itself is kept unchanged. Water years are named by the year they end in (2015 = 2014-15).
MANUAL_EXCLUSIONS = {
    # Maryknoll Fields: outages during big storms (Dec 2014, Feb 2019) left these years several
    # inches too low, although they pass the 95% rule.
    "6144": {
        2015: "excluded by hand: outage during the Dec 2014 storms left the total several inches too low",
        2019: "excluded by hand: outage during the Feb 2019 storms left the total several inches too low",
    },
}

HOUR_FORMAT = "%Y-%m-%d %H:%M"


# ---------- gauges and dates ----------

def load_gauges(path=GAUGES_FILE):
    with open(path, newline="") as f:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f) if (row.get("id") or "").strip()]


def now_pst():
    return datetime.now(PST).replace(tzinfo=None)


def hour_of(stamp):
    """The hour a 15-minute value belongs to, labeled by its start (a value stamped 02:00 belongs to 01:00)."""
    return (stamp - timedelta(minutes=1)).replace(minute=0, second=0, microsecond=0)


def water_year(day):
    """Water year number, named by the year it ends in (Oct 2025 - Sep 2026 is 2026)."""
    return day.year + 1 if day.month >= 10 else day.year


def wy_label(year):
    return f"{year - 1}-{year % 100:02d}"


def wy_start(year):
    return date(year - 1, 10, 1)


def wy_end(year):
    return date(year, 9, 30)


def days_between(first, last):
    d = first
    while d <= last:
        yield d
        d += timedelta(days=1)


def hours_between(first, last):
    h = first
    while h <= last:
        yield h
        h += timedelta(hours=1)


def fmt_rain(value):
    """Rain amounts are saved with 4 decimals so that adding them up stays accurate."""
    if value is None:
        return ""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def to_float(text):
    text = (text or "").strip()
    return float(text) if text else None


# ---------- talking to the API ----------

class Pacer:
    """Makes sure calls are at least `gap` seconds apart."""

    def __init__(self, gap, sleep=time.sleep, clock=time.monotonic):
        self.gap, self.sleep, self.clock, self.last = gap, sleep, clock, None

    def wait(self):
        if self.last is not None:
            remaining = self.gap - (self.clock() - self.last)
            if remaining > 0:
                self.sleep(remaining)

    def done(self):
        self.last = self.clock()


def parse_response(payload, gauge_id):
    """Return [(time in PST, inches)] from an API response. Raises ValueError if the API reported a problem."""
    meta = payload.get("metadata") or {}
    if str(meta.get("id")) != str(gauge_id):
        # The API answers problems with an empty result and an explanation in "comments".
        raise ValueError("API said: " + " ".join(payload.get("comments") or ["unexpected response"]))
    readings = []
    for rec in payload.get("data") or []:
        if rec.get("value") is None:
            continue
        stamp = datetime.fromisoformat(rec["timestamp"])
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(PST).replace(tzinfo=None)
        readings.append((stamp, float(rec["value"])))
    return readings


def fetch_rain(gauge_id, start, end, pacer, log=print, sleep=time.sleep, opener=urllib.request.urlopen, on_payload=None):
    """Get non-zero 15-minute rain values for one gauge, for whole days from `start` to `end` (inclusive).

    One call; on an error or timeout it tries again at most twice (after 30 s, then 2 min), then gives up.
    """
    url = f"{API}/SensorData/{gauge_id}/range/{start:%Y-%m-%d}/{end:%Y-%m-%d}?skipZeroValues=true"
    error = None
    for attempt, wait in enumerate((0,) + RETRY_WAITS, start=1):
        if wait:
            log(f"  {gauge_id}: {error}. Trying again in {wait} seconds.")
            sleep(wait)
        pacer.wait()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with opener(req, timeout=TIMEOUT_SECONDS) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            readings = parse_response(payload, gauge_id)
            if on_payload:
                on_payload(payload)
            return readings
        except Exception as e:  # network errors, timeouts, bad answers
            error = e
        finally:
            pacer.done()
    raise RuntimeError(f"gave up after {attempt} tries ({error})")


# ---------- hourly files: data/<id>.csv ----------

def hourly_totals(readings, first_hour, last_hour):
    """Rain per hour for every hour from first_hour to last_hour (0 if no rain was reported).

    Values outside that range are ignored, as are negative values (they aren't possible).
    """
    totals = {h: 0.0 for h in hours_between(first_hour, last_hour)}
    for stamp, value in readings:
        h = hour_of(stamp)
        if h in totals and value > 0:
            totals[h] += value
    return totals


def read_hourly(path):
    """{hour: rain} from an hourly file. Rain is None for hours with no data."""
    if not Path(path).exists():
        return {}
    with open(path, newline="") as f:
        return {datetime.strptime(r["hour_pst"], HOUR_FORMAT): to_float(r["rain_in_hour"]) for r in csv.DictReader(f)}


def write_hourly(path, rain, first_hour, last_hour):
    """Write one row per hour. The season total is the running sum since Oct 1, and is blank for hours with no data."""
    with open(path, "w", newline="") as f:
        out = csv.writer(f)
        out.writerow(["hour_pst", "season_total_in", "rain_in_hour"])
        season, total = None, 0.0
        for h in hours_between(first_hour, last_hour):
            if water_year(h) != season:
                season, total = water_year(h), 0.0
            value = rain.get(h)
            if value is not None:
                total += value
            out.writerow([h.strftime(HOUR_FORMAT), "" if value is None else f"{total:.2f}", fmt_rain(value)])


def day_total(rain, day):
    """Rain on one day from hourly values: the sum of the hours we have, or None if we have none."""
    values = [rain.get(datetime.combine(day, dtime(hour))) for hour in range(24)]
    known = [v for v in values if v is not None]
    return sum(known) if known else None


# ---------- daily history files: data/history/<id>_daily.csv ----------

def read_daily(path):
    if not Path(path).exists():
        return {}
    with open(path, newline="") as f:
        return {date.fromisoformat(r["date"]): to_float(r["rain_in"]) for r in csv.DictReader(f)}


def write_daily(path, daily, first_day, last_day):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        out = csv.writer(f)
        out.writerow(["date", "rain_in"])
        for d in days_between(first_day, last_day):
            out.writerow([d.isoformat(), fmt_rain(daily.get(d))])


# ---------- statistics: data/stats.json ----------

def suspected_outages(gid, daily_by_gauge, first_day, last_day):
    """Stretches of days when this gauge recorded no rain but the other gauges clearly did.

    The API reports 0 when a gauge isn't reporting, so outages look like dry spells.
    A dry spell counts as a suspected outage if, on the median of the other gauges,
    one of its days had at least 0.5 in, or the whole spell had at least 1 in.
    Returns [(first day, last day)].
    """
    mine = daily_by_gauge[gid]
    others = [daily_by_gauge[o] for o in daily_by_gauge if o != gid]
    found, run = [], []
    for d in list(days_between(first_day, last_day)) + [None]:
        if d is not None and mine.get(d) == 0:
            run.append(d)
            continue
        if run and others:
            day_medians = []
            for x in run:
                vals = [o[x] for o in others if o.get(x) is not None]
                if vals:
                    day_medians.append(statistics.median(vals))
            run_totals = [sum(o.get(x) or 0 for x in run) for o in others if any(o.get(x) is not None for x in run)]
            if (day_medians and max(day_medians) >= OUTAGE_DAY_IN) or (run_totals and statistics.median(run_totals) >= OUTAGE_RUN_IN):
                found.append((run[0], run[-1]))
        run = []
    return found


def season_slots():
    """The 366 days of a water year as (month, day), Oct 1 to Sep 30, including Feb 29."""
    return [(d.month, d.day) for d in days_between(date(2023, 10, 1), date(2024, 9, 30))]


def cumulative_by_slot(daily, year):
    """Running total since Oct 1 for each day slot of one water year (missing days count as 0).

    In years without Feb 29, that slot repeats Feb 28's total.
    """
    running, by_day = 0.0, {}
    for d in days_between(wy_start(year), wy_end(year)):
        running += daily.get(d) or 0.0
        by_day[(d.month, d.day)] = running
    return [by_day.get(s, by_day.get((2, 28))) for s in season_slots()]


def compute_stats(gauges, daily_by_gauge, today):
    current = water_year(today)
    years = list(range(current - HISTORY_YEARS, current))  # complete prior water years only
    last_year = current - 1
    result = {
        "units": "inches",
        "notes": [
            "Water years run Oct 1 to Sep 30. Values are cumulative rainfall since Oct 1, by day of the water year.",
            "Days are PST, as reported by the district.",
            f"Averages use complete water years from the last {HISTORY_YEARS} (at least {COMPLETE_SHARE:.0%} of days with data). "
            "The current water year is never included.",
            "Feb 29 in years without one repeats Feb 28's total.",
            "Suspected outages are dry spells when the other gauges clearly got rain; those days count as missing.",
        ],
        "current_water_year": wy_label(current),
        "last_water_year": wy_label(last_year),
        "days": [f"{m:02d}-{d:02d}" for m, d in season_slots()],
        "gauges": {},
    }
    for g in gauges:
        gid = g["id"]
        daily = daily_by_gauge.get(gid, {})
        first_data = min((d for d, v in daily.items() if v is not None), default=None)
        outages = suspected_outages(gid, daily_by_gauge, wy_start(years[0]), today) if daily else []
        outage_days = {d for a, b in outages for d in days_between(a, b)}

        complete, excluded, coverage_of = [], [], {}
        for y in years:
            days = list(days_between(wy_start(y), wy_end(y)))
            have = sum(1 for d in days if daily.get(d) is not None and d not in outage_days)
            coverage_of[y] = have / len(days)
            manual = MANUAL_EXCLUSIONS.get(gid, {}).get(y)
            if manual:
                excluded.append({"water_year": wy_label(y), "coverage_percent": round(coverage_of[y] * 100, 1), "reason": manual})
                continue
            if coverage_of[y] >= COMPLETE_SHARE:
                complete.append(y)
                continue
            if have == 0 and (first_data is None or first_data > wy_end(y)):
                reason = "no data" + (f" (the gauge's records start {first_data.isoformat()})" if first_data else "")
            else:
                missing = sum(1 for d in days if d in outage_days)
                reason = f"only {coverage_of[y]:.0%} of days have data"
                if missing:
                    reason += f" ({missing} days in suspected outages)"
            excluded.append({"water_year": wy_label(y), "coverage_percent": round(coverage_of[y] * 100, 1), "reason": reason})

        curves = [cumulative_by_slot(daily, y) for y in complete]
        stat = lambda fn: [round(fn(vals), 2) for vals in zip(*curves)] if curves else None
        totals = [c[-1] for c in curves]
        last = cumulative_by_slot(daily, last_year) if any(daily.get(d) is not None for d in days_between(wy_start(last_year), wy_end(last_year))) else None
        result["gauges"][gid] = {
            "name": g.get("name", gid),
            "limited_history": len(complete) < LIMITED_YEARS,
            "years_used": len(complete),
            "water_years_used": [wy_label(y) for y in complete],
            "date_range": {"from": wy_start(complete[0]).isoformat(), "to": wy_end(complete[-1]).isoformat()} if complete else None,
            "excluded_years": excluded,
            "suspected_outages": [{"from": a.isoformat(), "to": b.isoformat(), "days": (b - a).days + 1} for a, b in outages],
            "average_season_total": round(statistics.mean(totals), 2) if totals else None,
            "last_year_complete": last_year in complete,
            "average": stat(statistics.mean),
            "min": stat(min),
            "max": stat(max),
            "last_year": [round(v, 2) for v in last] if last else None,
        }
    return result


def write_stats(gauges, today, path=None):
    path = path or STATS_FILE
    daily_by_gauge = {g["id"]: read_daily(HISTORY_DIR / f"{g['id']}_daily.csv") for g in gauges}
    stats = compute_stats(gauges, daily_by_gauge, today)
    text = json.dumps(stats, indent=1)
    # Keep each list of plain values on one line so the file stays easy to read.
    text = re.sub(r"\[\n\s+([^\[\]{}]*?)\n\s*\]", lambda m: "[" + re.sub(r",\n\s*", ", ", m.group(1)) + "]", text)
    Path(path).write_text(text + "\n")
    return stats
