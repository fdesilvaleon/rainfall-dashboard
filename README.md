# Mountain View Rainfall Dashboard

This project collects rainfall readings for the Mountain View area from the
Santa Clara Valley Water District's ALERT rain gauges, using the district's
[Surface Water Data API](https://alertdata.valleywater.org/api.html).

**Dashboard:** https://fdesilvaleon.github.io/rainfall-dashboard/

The dashboard (`index.html`) shows a map of the gauge locations, how each
gauge's season compares with normal and with last year (a table for all gauges,
and numbers and a water-year chart for a chosen gauge), hourly rain, and recent
totals. The map uses standard OpenStreetMap tiles (tile.openstreetmap.org),
shown in gray at low contrast. It is hosted free on GitHub Pages and reads the data files in this
repository (`data/<id>.csv`, `data/history/` and `data/stats.json`), so it
updates whenever new data is collected.

> **Not for safety decisions.** For emergencies, flood warnings and safety
> information, check the Santa Clara Valley Water website
> ([valleywater.org](https://www.valleywater.org/)) and official local alerts.

## How it works

- **`gauges.csv`** is the list of rain gauges. To add a gauge, add a line with
  its ID number, name, latitude, longitude and (optionally) a short `label`
  for the map. IDs and coordinates are in the
  district's sensor list at https://alertdata.valleywater.org/Sensor/all (look
  up the gauge's `gageId` and copy its `latitude` and `longitude`). Then run
  `python backfill.py <id>` once to download its history.
- **`collect.py`** runs every hour on GitHub Actions (at 17 minutes past the
  hour). For each gauge it asks the API for the last 48 hours of rain and
  updates the files below. If recent hours are missing (for example after
  GitHub skipped some runs), the same request simply starts earlier, up to 30
  days back. It then recomputes `data/stats.json`.
- **`backfill.py`** downloads the history (the last 15 full water years). It
  was run once by hand and is not scheduled.
- **`rainfall.py`** holds the code the two scripts share.
- **`test_rainfall.py`** checks the code works without contacting the API
  (run `python -m unittest`). GitHub Actions runs it before each collection.
- **`legacy/`** holds the old collector, which read the district's report
  pages. It is kept for reference only and is not scheduled.

You can also run the collector any time from the **Actions** tab ("Collect
rainfall data" → "Run workflow"), for example during a storm.

### Polite access to the district's API

The district asked that requests stay at about one every few minutes at most.

- Each hourly run makes **one request per gauge** (7 in all), at least
  **5 seconds apart**, and asks only for the last 48 hours.
- If a request fails or times out, it is tried again **at most twice**, after
  30 seconds and then 2 minutes. If it still fails, that gauge is skipped until
  the next hour, which catches up.
- Requests ask only for 15-minute periods with rain (`skipZeroValues=true`),
  which keeps answers small.
- Every request identifies the project with the User-Agent
  `rainfall-dashboard personal project (github.com/fdesilvaleon/rainfall-dashboard)`.
- The history download made one request per gauge, at least 10 seconds apart.

## About the data

**Water years** run from **Oct 1 to Sep 30** and are named by both years, for
example "2025-26". All times and days are **Pacific Standard Time (PST) all year**,
as the district reports them.

**Note:** the API reports 0 both for dry periods and for times when a gauge
isn't reporting, so a gauge outage can look like a dry spell.

The API gives the rain in each 15-minute period. A value stamped 02:00 is the
rain from 01:45 to 02:00, so it counts in the 01:00 hour (and a value stamped
midnight counts in the day before).

### `data/<gauge id>.csv`: hourly, current and previous water year

- `hour_pst`: the start of the hour (`08:00` covers 08:00 to 08:59).
- `season_total_in`: rain since Oct 1, in inches (rounded to 2 decimals, like
  the district's portal).
- `rain_in_hour`: rain in that hour, in inches (4 decimals, so sums stay
  accurate).

Blank values mean there is no data for that hour. The most recent hour is
updated on later runs as it fills in, and the last 48 hours are refreshed every
run, so late or corrected readings are picked up.

### `data/history/<gauge id>_daily.csv`: daily, last 15 full water years

Columns `date` and `rain_in` (inches), from Oct 1, 2011, updated every hour.
A blank value means no data (before the gauge's records start).

### `data/stats.json`: statistics for the website

Recomputed every run. For each gauge, and for each day of the water year
(Oct 1 to Sep 30, including Feb 29), it has:

- `average`, `min`, `max`: rain since Oct 1 up to that day, across **complete**
  prior water years;
- `last_year`: the same for last water year;
- `average_season_total`, `years_used`, `water_years_used`, `date_range`;
- `excluded_years` (with the reason), `suspected_outages`, and
  `limited_history` (true if fewer than 5 complete years).

A water year is **complete** if at least 95% of its days have data. Only the
last 15 full water years are considered, and the current (unfinished) water
year is never included.

**Years excluded by hand** (listed in `MANUAL_EXCLUSIONS` in `rainfall.py`; the
daily data is kept unchanged):

- Maryknoll Fields (6144): 2014-15 and 2018-19. They pass the 95% rule, but
  outages during big storms (Dec 2014, Feb 2019) left them several inches too low.

**Missing data:** because outages look like dry spells (see the note above), a dry spell is
treated as a **suspected outage** (and its days count as missing) if the other
gauges clearly got rain at the same time: a median of at least 0.5 in on one
day, or at least 1 in over the spell.

The data is preliminary and may contain errors, as noted by the district.

### `data/forecast.json`: 7-day rain forecast (National Weather Service)

Updated every hour by `forecast.py`, after the rainfall data is saved, from the
free NWS API (https://api.weather.gov). For each gauge it uses the raw forecast
for the 2.5 km grid cell the gauge is in (`/gridpoints/{office}/{x},{y}`):
expected rain (`quantitativePrecipitation`, given in millimetres) and chance of
rain (`probabilityOfPrecipitation`). Each gauge's grid cell is looked up once
(`/points/{lat},{lon}`) and saved in `data/forecast_grids.json`.

For each gauge:

- `grid`: the NWS grid cell used (forecast office, x, y).
- `nws_updated`: when NWS last updated that forecast (UTC, as NWS gives it).
- `fetched_at`: when we fetched it (Pacific time).
- `days`: the next 7 calendar days in **Pacific local time** (with daylight
  saving time, unlike the gauge data, which is PST all year), starting today:
  - `rain_in`: expected rain that day, in inches: the sum of the forecast
    periods counted for that day. A period that crosses midnight (NWS uses
    6-hour periods, one of which runs about 10 PM to 4 AM) counts for the day
    that holds most of its hours. Amounts are converted from millimetres but not
    otherwise rounded or smoothed.
  - **`rain_in` is `null` (unknown), not 0,** if NWS gave no amount for any
    period counted for that day, or if no period was counted (beyond the end of
    the forecast).
  - `complete`: `false` when the forecast does not cover the whole day.
    Today is always partial, because the forecast starts at its latest update
    (and may include hours earlier today that have already passed).
  - `hours_covered`: how many hours of that day the forecast covers.
  - `chance_max_pct`: the highest chance of rain (%) among the forecast periods
    that overlap the day. A chance of rain can come with 0.00 in expected, when
    only a trace is likely.
  - `periods`: the forecast periods counted for that day, as NWS gave them.

Top level: `fetched_at` (the last time a new forecast was fetched) and
`last_error` (`null`, or the time and message of the last problem).

**If the forecast can't be fetched,** the previous `forecast.json` is kept, the
problem is recorded in `last_error`, and the run carries on: the forecast never
stops the rainfall data from being saved. The file is only rewritten when its
contents change.

**Polite access to the NWS API:** at least 5 seconds between requests, one
request per grid cell per run, at most 2 retries (after 30 seconds, then 2
minutes), and if a request still fails no more NWS requests are made until the
next run. Requests use the same User-Agent as the rest of the project.

## Attribution and disclaimer

Data: Santa Clara Valley Water ALERT system
([alert.valleywater.org](https://alert.valleywater.org/)). Readings are
preliminary and unreviewed, may contain errors or gaps, and may be revised by
the district. This is an independent personal project and is not affiliated
with or endorsed by Santa Clara Valley Water. Data is provided as is, with no
guarantee of accuracy, completeness or timeliness, and the author is not
responsible for any decisions made from it. Map data and map tiles ©
[OpenStreetMap contributors](https://www.openstreetmap.org/copyright).
