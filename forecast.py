"""Forecast step: expected rain for the next 7 days at each gauge, from the National Weather Service.

Uses the free NWS API (https://api.weather.gov):
  /points/{lat},{lon}            finds the gauge's 2.5 km forecast grid cell (done once, saved in
                                 data/forecast_grids.json)
  /gridpoints/{office}/{x},{y}   the raw forecast for that cell; we use quantitativePrecipitation
                                 (expected rain, in mm), probabilityOfPrecipitation (%) and updateTime

Writes data/forecast.json. This step never stops a run: if anything fails, the previous
forecast.json is kept and the error is recorded inside it.

Polite access: at least 5 seconds between requests, at most 2 retries (after 30 s, then 2 min),
one request per distinct grid cell per run, and if a request still fails we stop asking NWS
until the next run.
"""
import json
import re
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import rainfall as rf

NWS = "https://api.weather.gov"
LOCAL = ZoneInfo("America/Los_Angeles")  # Pacific local time, with daylight saving time
DAYS = 7
GAP_SECONDS = 5
MM_PER_INCH = 25.4

NOTES = [
    "Source: National Weather Service raw gridpoint forecast (api.weather.gov), one 2.5 km grid cell per gauge.",
    "Days are calendar days in Pacific local time (America/Los_Angeles, with daylight saving time).",
    "rain_in: expected rain that day in inches, the sum of the forecast periods counted for that day. "
    "A period that crosses midnight counts for the day that holds most of its hours.",
    "rain_in is null if NWS gave no amount for a period counted for that day, or if no period was counted.",
    "complete is false when the forecast does not cover the whole day (today, and sometimes the last day).",
    "chance_max_pct: the highest chance of rain among the forecast periods that overlap the day.",
    "periods lists the forecast periods counted for each day, as NWS gave them (converted from mm to inches).",
]


def forecast_file():
    return rf.DATA_DIR / "forecast.json"


def grids_file():
    return rf.DATA_DIR / "forecast_grids.json"


def now_local():
    return datetime.now(LOCAL)


# ---------- talking to NWS ----------

def get_json(url, pacer, log=print, sleep=time.sleep, opener=urllib.request.urlopen):
    """One GET request; on an error or timeout try again at most twice (after 30 s, then 2 min), then give up."""
    error = None
    for attempt, wait in enumerate((0,) + rf.RETRY_WAITS, start=1):
        if wait:
            log(f"  forecast: {error}. Trying again in {wait} seconds.")
            sleep(wait)
        pacer.wait()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": rf.USER_AGENT, "Accept": "application/geo+json"})
            with opener(req, timeout=rf.TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # network errors, timeouts, error pages
            error = e
        finally:
            pacer.done()
    raise RuntimeError(f"gave up after {attempt} tries ({error})")


# ---------- reading the forecast ----------

DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


def parse_interval(text):
    """'2026-11-13T18:00:00+00:00/PT6H' -> (start, end) as UTC datetimes."""
    start_text, rest = text.split("/")
    start = datetime.fromisoformat(start_text).astimezone(timezone.utc)
    if rest.startswith("P"):
        m = DURATION.match(rest)
        if not m:
            raise ValueError(f"unexpected duration {rest!r}")
        d, h, mi = (int(x or 0) for x in m.groups())
        end = start + timedelta(days=d, hours=h, minutes=mi)
    else:
        end = datetime.fromisoformat(rest).astimezone(timezone.utc)
    return start, end


def layer(props, name, unit):
    """[(start, end, value)] from one forecast layer, converted to `unit` ("in" or "percent")."""
    found = props.get(name)
    if found is None:
        raise ValueError(f"the forecast has no {name}")
    uom = found.get("uom", "")
    if unit == "in":
        factor = {"wmoUnit:mm": 1 / MM_PER_INCH, "wmoUnit:in": 1}.get(uom)
    else:
        factor = 1 if uom == "wmoUnit:percent" else None
    if factor is None:
        raise ValueError(f"unexpected unit {uom!r} for {name}")
    out = []
    for item in found.get("values", []):
        start, end = parse_interval(item["validTime"])
        value = item.get("value")
        out.append((start, end, None if value is None else value * factor))
    return out


# ---------- feels-like temperature ----------

def hourly(props, name, convert):
    """{hour start (UTC): value} from one forecast layer, with values converted by convert[uom].
    Missing layers or unknown units give {}."""
    found = props.get(name)
    if not found or found.get("uom") not in convert:
        return {}
    to = convert[found["uom"]]
    out = {}
    for item in found.get("values", []):
        if item.get("value") is None:
            continue
        start, end = parse_interval(item["validTime"])
        h = start.replace(minute=0, second=0, microsecond=0)
        while h < end:
            out[h] = to(item["value"])
            h += timedelta(hours=1)
    return out


def c_to_f(c):
    return c * 9 / 5 + 32


def computed_feels_like(temp_f, wind_mph, humidity):
    """NWS formulas: wind chill at 50 F or below with wind of 3 mph or more, heat index at 80 F or above,
    otherwise the air temperature. Returns None if a needed input is missing."""
    if temp_f is None:
        return None
    if temp_f <= 50:
        if wind_mph is None:
            return None
        if wind_mph < 3:
            return temp_f
        v = wind_mph ** 0.16
        return 35.74 + 0.6215 * temp_f - 35.75 * v + 0.4275 * temp_f * v
    if temp_f >= 80:
        if humidity is None:
            return None
        t, rh = temp_f, humidity
        hi = 0.5 * (t + 61 + (t - 68) * 1.2 + rh * 0.094)
        if (hi + t) / 2 < 80:
            return hi
        hi = (-42.379 + 2.04901523 * t + 10.14333127 * rh - 0.22475541 * t * rh - 0.00683783 * t * t
              - 0.05481717 * rh * rh + 0.00122874 * t * t * rh + 0.00085282 * t * rh * rh - 0.00000199 * t * t * rh * rh)
        if rh < 13 and 80 <= t <= 112:
            hi -= ((13 - rh) / 4) * ((17 - abs(t - 95)) / 17) ** 0.5
        elif rh > 85 and 80 <= t <= 87:
            hi += ((rh - 85) / 10) * ((87 - t) / 5)
        return hi
    return temp_f


def feels_like_hours(props):
    """{hour start (UTC): feels-like in F}. Uses NWS's apparentTemperature; for hours where it's missing,
    computes wind chill or heat index from temperature, wind speed and humidity."""
    apparent = hourly(props, "apparentTemperature", {"wmoUnit:degC": c_to_f, "wmoUnit:degF": lambda v: v})
    temp = hourly(props, "temperature", {"wmoUnit:degC": c_to_f, "wmoUnit:degF": lambda v: v})
    wind = hourly(props, "windSpeed", {"wmoUnit:km_h-1": lambda v: v * 0.621371, "wmoUnit:m_s-1": lambda v: v * 2.236936,
                                       "wmoUnit:mi_h-1": lambda v: v})
    humidity = hourly(props, "relativeHumidity", {"wmoUnit:percent": lambda v: v})
    out = dict(apparent)
    for h in temp:
        if h not in out:
            value = computed_feels_like(temp[h], wind.get(h), humidity.get(h))
            if value is not None:
                out[h] = value
    return out


def average_feels_like(feels, start, end):
    """Average feels-like (F) over the hours of one period, or None if no hour has a value."""
    values = []
    h = start
    while h < end:
        if h in feels:
            values.append(feels[h])
        h += timedelta(hours=1)
    return round(sum(values) / len(values), 1) if values else None


def day_bounds(day):
    """Start and end of a local calendar day, in UTC (23 or 25 hours long when clocks change)."""
    start = datetime(day.year, day.month, day.day, tzinfo=LOCAL)
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=LOCAL)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def overlap_hours(a_start, a_end, b_start, b_end):
    return max(0.0, (min(a_end, b_end) - max(a_start, b_start)).total_seconds() / 3600)


def home_day(start, end):
    """The local day holding most of a period's hours (on a tie, the earlier day)."""
    best, best_hours = None, -1
    day = start.astimezone(LOCAL).date()
    while True:
        ds, de = day_bounds(day)
        if ds >= end:
            return best
        hours = overlap_hours(start, end, ds, de)
        if hours > best_hours:
            best, best_hours = day, hours
        day += timedelta(days=1)


def summarize(props, today):
    """The next DAYS local days, starting with `today`."""
    rain = layer(props, "quantitativePrecipitation", "in")
    chance = layer(props, "probabilityOfPrecipitation", "percent")
    feels = feels_like_hours(props)
    window = (min(s for s, _, _ in rain), max(e for _, e, _ in rain)) if rain else None
    days = []
    for k in range(DAYS):
        day = today + timedelta(days=k)
        ds, de = day_bounds(day)
        mine = [(s, e, v) for s, e, v in rain if home_day(s, e) == day]
        unknown = any(v is None for _, _, v in mine)
        chances = [v for s, e, v in chance if v is not None and s < de and e > ds]
        days.append({
            "date": day.isoformat(),
            # Amounts as NWS gave them (hundredths of an inch); rounding only removes floating-point noise.
            "rain_in": None if (not mine or unknown) else round(sum(v for _, _, v in mine), 4),
            "complete": bool(mine) and not unknown and window is not None and window[0] <= ds and window[1] >= de,
            "hours_covered": round(overlap_hours(ds, de, *window), 2) if window else 0,
            "chance_max_pct": max(chances) if chances else None,
            "periods": [{
                "start": s.astimezone(LOCAL).isoformat(timespec="minutes"),
                "hours": round((e - s).total_seconds() / 3600, 2),
                "rain_in": None if v is None else round(v, 4),
                "apparent_temp_f": average_feels_like(feels, s, e),  # feels-like, averaged over the period
            } for s, e, v in mine],
        })
    return days


# ---------- the step ----------

def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return None


def strip(x, keys=("fetched_at",)):
    """The data without the given keys (at any depth), for comparing contents."""
    if isinstance(x, dict):
        return {k: strip(v, keys) for k, v in x.items() if k not in keys}
    if isinstance(x, list):
        return [strip(v, keys) for v in x]
    return x


def write_if_changed(path, data):
    """Write the file only if something other than fetch times changed. Returns True if written."""
    old = read_json(path)
    if old is not None and strip(old) == strip(data):
        return False
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=1) + "\n")
    return True


def update(gauges, now=None, fetch=None, log=print):
    """Refresh data/forecast.json. Never raises: on failure the previous forecast is kept and the error recorded."""
    now = now or now_local()
    stamp = now.isoformat(timespec="seconds")
    previous = read_json(forecast_file()) or {}
    try:
        if fetch is None:
            pacer = rf.Pacer(GAP_SECONDS)
            fetch = lambda url: get_json(url, pacer, log=log)
        out, error = build(gauges, now, fetch, previous, log)
    except Exception as e:  # anything unexpected: keep what we had
        out, error = dict(previous) if previous else {"gauges": {}}, f"{type(e).__name__}: {e}"
    status = ("checked" if not error else "problem")
    if not error and previous and strip(previous, ("fetched_at", "checked_at", "last_error")) == strip(out, ("fetched_at", "checked_at", "last_error")):
        # Same forecast as before: keep the earlier fetch times, so fetched_at still means
        # "when this forecast was fetched". checked_at (below) records this check.
        out = json.loads(json.dumps(previous))
        status = "no change"
    elif not error:
        status = "updated"
    # checked_at: the last time NWS was checked successfully. A failed check keeps the previous one.
    out["checked_at"] = previous.get("checked_at") if error else stamp
    out["last_error"] = {"time": stamp, "message": error} if error else None
    write_if_changed(forecast_file(), out)
    log(f"Forecast: {status}" + (f" ({error})" if error else ""))
    return out


def build(gauges, now, fetch, previous, log):
    """Returns (forecast data, error message or None)."""
    grids = read_json(grids_file()) or {}
    old = previous.get("gauges", {})
    out = {"notes": NOTES, "time_zone": "America/Los_Angeles", "fetched_at": now.isoformat(timespec="seconds"), "gauges": {}}
    cells, error, fresh = {}, None, 0
    for g in gauges:
        gid = g["id"]
        try:
            lat, lon = float(g["lat"]), float(g["lon"])
        except (KeyError, TypeError, ValueError):
            continue  # no coordinates for this gauge
        if error is None:
            try:
                # Grid cells are looked up once and saved; looked up again only if the gauge's coordinates change.
                cached = grids.get(gid)
                if not cached or cached.get("lat") != lat or cached.get("lon") != lon:
                    point = fetch(f"{NWS}/points/{lat:.4f},{lon:.4f}")["properties"]
                    grids[gid] = {"lat": lat, "lon": lon, "office": point["gridId"], "x": point["gridX"], "y": point["gridY"]}
                    write_if_changed(grids_file(), grids)
                cell = (grids[gid]["office"], grids[gid]["x"], grids[gid]["y"])
                if cell not in cells:  # gauges in the same grid cell share one request
                    data = fetch(f"{NWS}/gridpoints/{cell[0]}/{cell[1]},{cell[2]}")["properties"]
                    cells[cell] = {"data": data, "days": summarize(data, now.astimezone(LOCAL).date())}
                out["gauges"][gid] = {
                    "name": g.get("name", gid),
                    "grid": {"office": cell[0], "x": cell[1], "y": cell[2]},
                    "nws_updated": cells[cell]["data"].get("updateTime"),
                    "fetched_at": now.isoformat(timespec="seconds"),
                    "days": cells[cell]["days"],
                }
                fresh += 1
                continue
            except Exception as e:
                error = f"gauge {gid}: {e}"  # stop asking NWS for the rest of this run
        if gid in old:
            out["gauges"][gid] = old[gid]  # keep the previous forecast for this gauge
    if fresh == 0 and previous.get("fetched_at"):
        out["fetched_at"] = previous["fetched_at"]  # nothing new was fetched this run
    return out, error
