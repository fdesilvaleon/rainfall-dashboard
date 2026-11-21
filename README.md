# Trail Conditions

Trail Conditions rates running and biking conditions for the Peninsula and South
Bay, from the Santa Clara Valley Water District's ALERT rain gauges (using the
district's [Surface Water Data API](https://alertdata.valleywater.org/api.html))
and National Weather Service forecasts. The rainfall data and charts behind the
ratings are still part of the page.

**Dashboard:** https://fdesilvaleon.github.io/rainfall-dashboard/

The dashboard (`index.html`) shows a trail conditions map and ratings for each
trail region, and the rainfall itself: how each gauge's season compares with normal and with last year
(a "% of normal" chart by trail region, with each gauge's numbers under
"Gauge details", and numbers and a water-year chart for a chosen gauge, picked
from a list grouped by region),
hourly rain, and recent totals. The map uses standard OpenStreetMap tiles
(tile.openstreetmap.org), shown in gray at low contrast. It is hosted free on GitHub Pages and reads the data files in this
repository (`data/<id>.csv`, `data/history/` and `data/stats.json`), so it
updates whenever new data is collected.

> **Not for safety decisions.** For emergencies, flood warnings and safety
> information, check the Santa Clara Valley Water website
> ([valleywater.org](https://www.valleywater.org/)) and official local alerts.

## How it works

- **`gauges.csv`** is the list of rain gauges. To add a gauge, add a line with
  its ID number, name, latitude, longitude and (optionally) a short `label`
  (used in chart labels). IDs and coordinates are in the
  district's sensor list at https://alertdata.valleywater.org/Sensor/all (look
  up the gauge's `gageId` and copy its `latitude` and `longitude`). Then run
  `python backfill.py <id>` once to download its history.
- **`collect.py`** runs every hour on GitHub Actions (at 17 minutes past the
  hour). For each gauge it asks the API for the last 48 hours of rain and
  updates the files below. If recent hours are missing (for example after
  GitHub skipped some runs), the same request simply starts earlier, up to 30
  days back. It then recomputes `data/stats.json`.
- **`backfill.py`** downloads the history (the last 15 full water years). It
  was run once by hand and is not scheduled. With `--all-years` it keeps all of
  a gauge's records instead (`python backfill.py --all-years <id>`); this was
  used for the 9 gauges added in Nov 2026.
- **`rainfall.py`** holds the code the two scripts share.
- **`test_rainfall.py`** checks the code works without contacting the API
  (run `python -m unittest`). GitHub Actions runs it before each collection.
- **`legacy/`** holds the old collector, which read the district's report
  pages. It is kept for reference only and is not scheduled.

You can also run the collector any time from the **Actions** tab ("Collect
rainfall data" → "Run workflow"), for example during a storm.

### Polite access to the district's API

The district asked that requests stay at about one every few minutes at most.

- Each hourly run makes **one request per gauge** (16 in all), at least
  **5 seconds apart**, and asks only for the last 48 hours.
- If a request fails or times out, it is tried again **at most twice**, after
  30 seconds and then 2 minutes. If it still fails, that gauge is skipped until
  the next hour, which catches up.
- Requests ask only for 15-minute periods with rain (`skipZeroValues=true`),
  which keeps answers small.
- Every request identifies the project with the User-Agent
  `rainfall-dashboard personal project (github.com/fdesilvaleon/rainfall-dashboard)`.
- The history download made one request per gauge, at least 10 seconds apart.

## The gauges

The first 7 are the original Mountain View-area gauges; the other 9 were added
in Nov 2026 (foothills west of Mountain View and Palo Alto, and the south San
Jose and Morgan Hill area). "Records start" is the first rain in the API; for the
original 7 only the last 15 water years are stored.

| ID | Name | Records start (API) |
|---|---|---|
| 6121 | Mountain View | Aug 1992 |
| 6140 | Rancho San Antonio | Jan 2012 |
| 6100 | Stevens Creek Reservoir | Mar 1979 |
| 6048 | Sunnyvale Hamilton WTP | Nov 1974 |
| 6129 | Palo Alto | Feb 1980 |
| 6144 | Maryknoll Fields | Sep 2013 |
| 6142 | Trappers Trail | Sep 2013 |
| 6147 | Westwind Community Barn | Nov 2017 |
| 6146 | Los Trancos | Feb 2014 |
| 6148 | Windy Hill | Jul 2022 |
| 6149 | Wunderlich | Sep 2022 |
| 6150 | Huddart Park | Jan 2023 |
| 6123 | Guadalupe Watershed | Oct 1991 |
| 6004 | Almaden Watershed | Apr 1982 |
| 6128 | Calero Watershed | Mar 1980 |
| 6141 | Palm Ave | Sep 2013 |

Windy Hill, Wunderlich and Huddart Park are owned by the San Francisquito Creek
Joint Powers Authority and maintained by Valley Water.

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
run, so late readings are picked up.

**Saved rain is protected.** Valley Water's API reports 0 both when it's dry and
when a gauge isn't reporting, and some runs get an answer with no rain at all for
hours that did have rain (on Nov 14, 2026, several runs briefly wiped Nov 13's
rain for two or three gauges each). So when a run re-fetches an hour that already
has rain saved, a lower value (0 or smaller) is ignored and the saved value kept;
the run's log says so ("API returned 0 for N hours that already had rain; kept
saved values"). Larger values and hours with no saved rain go in as usual. The
daily history and `stats.json` are built from this protected data, so they're
protected too. **Trade-off:** if Valley Water ever legitimately corrects a value
downward, the saved value stays until it is fixed by hand in `data/<gauge id>.csv`
(and the matching day in `data/history/<gauge id>_daily.csv`).

### `data/history/<gauge id>_daily.csv`: daily history

Columns `date` and `rain_in` (inches), updated every hour. The original 7 gauges
start on Oct 1, 2011 (the last 15 full water years). The 9 gauges added in Nov
2026 keep **all** their records: from Oct 1 of their first water year (Guadalupe
1991, Almaden 1981, Calero 1979), or Oct 1, 2011 if their records start later.
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
- Los Trancos (6146): 2013-14 (partial first year) and 2014-15 (offline Jun 2014
  to Oct 2015).
- Westwind Community Barn (6147): 2017-18 (partial first year) and 2020-21
  (offline May 2020 to Oct 2021).
- Palm Ave (6141): 2012-13 (partial first year) and 2019-20 (offline May 2019
  to Nov 2020).
- Windy Hill (6148) and Wunderlich (6149): 2021-22 (partial first year).
- Huddart Park (6150): 2022-23 (partial first year).

The API reports the offline periods as zero rain, so they look like very dry
years. Partial first years would also fail the 95% rule on their own (as the
original gauges' first years do: 6140 2011-12, 6144 and 6142 2012-13); they are
listed by hand to make it explicit. The offline periods also reach into the
neighbouring years, which the 95% rule leaves out automatically: Westwind 2019-20
and Palm Ave 2018-19 and 2020-21. Windy Hill, Wunderlich and Huddart Park have
fewer than 5 complete years, so they show "limited history".

**Notes on the added gauges:**

- **Los Trancos (6146)** is not a Valley Water sensor, and Valley Water says its
  data has not been quality controlled.
- **Wunderlich (6149)** logged 4.02 in in one 15-minute reading on Jan 4, 2023,
  after recording much less than nearby gauges during the Dec 27-31 storms (most
  likely a clogged funnel emptying). Its daily amounts from Dec 27, 2022 to
  Jan 4, 2023 are unreliable, although the year's total looks fine.

**Missing data:** because outages look like dry spells (see the note above), a dry spell is
treated as a **suspected outage** (and its days count as missing) if the other
gauges clearly got rain at the same time: a median of at least 0.5 in on one
day, or at least 1 in over the spell. "The other gauges" are always the original
7 (`OUTAGE_REFERENCE` in `rainfall.py`), so adding gauges doesn't change the
results for the existing ones.

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
  - `periods`: the forecast periods counted for that day, as NWS gave them:
    `start`, `hours`, `rain_in`, and `apparent_temp_f`, the average "feels like"
    temperature over the period's hours in °F. It comes from NWS's
    `apparentTemperature` (converted from °C). For any hour where that's missing,
    it's computed from temperature, wind speed and humidity with the standard NWS
    formulas (wind chill at 50°F or below with wind of at least 3 mph, heat index
    at 80°F or above, otherwise the air temperature). It's `null` if neither is
    possible.

Top level: `fetched_at` (the last time a new forecast was fetched),
`checked_at` (the last time NWS was checked successfully, updated every hourly
run even when the forecast hasn't changed) and `last_error` (`null`, or the time
and message of the last problem).

**If the forecast can't be fetched,** the previous `forecast.json` is kept
(including its `checked_at`), the problem is recorded in `last_error`, and the
run carries on: the forecast never stops the rainfall data from being saved.
When NWS hasn't published anything new, only `checked_at` changes.

**On the dashboard:** NWS sometimes publishes nothing for several hours (often
overnight). If our last successful check was within 3 hours and there was no
error, a forecast issued more than 6 hours ago gets a neutral note ("NWS hasn't
published a newer one yet"). A warning that the forecast couldn't be refreshed
appears only when the last successful check is more than 3 hours old or the
last check failed.

**Polite access to the NWS API:** at least 5 seconds between requests, one
request per grid cell per run, at most 2 retries (after 30 seconds, then 2
minutes), and if a request still fails no more NWS requests are made until the
next run. Requests use the same User-Agent as the rest of the project.

### `data/forecast_history.csv`: a copy of every new forecast

The collector keeps a copy of each new NWS forecast it sees, so past forecasts
can later be compared with the rain the gauges measured. It comes from
`forecast_history.py`.

One row per gauge, per target day, per NWS issue time:

| Column | Meaning |
|---|---|
| `snapshot_time` | when NWS issued or last updated that forecast (UTC) |
| `saved_at` | when we saved it (UTC) |
| `gauge_id` | the gauge |
| `target_date` | the day the forecast is for (Pacific local date) |
| `lead_days` | `target_date` minus the Pacific local date of `snapshot_time` (0 = same day, 1 = next day, ...) |
| `expected_in` | expected rain that day, in inches; **blank, not 0, if NWS gave no amount** |
| `chance_pct` | highest chance of rain that day, in %; blank if unknown |
| `partial_day` | 1 if the forecast covered only part of that day (as marked in `forecast.json`), otherwise 0 |

**When rows are saved:** every collector run (hourly) looks at the forecast it
has just fetched. For each gauge, if NWS's issue time (`snapshot_time`) isn't in
the history yet for that gauge, its rows are added. The same issue time is never
saved twice, even if a run is repeated. Each gauge is checked on its own because
the NWS grid cells don't always update at the same moment. A missing forecast, a
failed fetch or one with a recorded error saves nothing, and the reason is
logged. A problem here never stops the rainfall data or the forecast from being
saved.

**Trimming:** only target days 0 to 3 days ahead are kept (up to 4 rows per
gauge, 28 per issue time). Rows saved before Nov 12, 2026 (two daily snapshots)
have all 7 days. Rows are only ever added at the end; old rows are never changed
or deleted.

**Backfill:** the forecasts issued between Nov 11 (1:36 PM Pacific) and Nov 12,
2026 were added from the earlier copies of `data/forecast.json` in this
repository's history (`python forecast_history.py --backfill-from-git`). Their
`saved_at` is when that copy was committed.

**Size:** NWS updated the forecast about 7 times a day in the first day of
records, so the file grows by about 200 rows (14 KB) a day, roughly 5 MB a year.

**What it can tell us:** how the forecast for a day changed as the day got
closer (rows with the same `gauge_id` and `target_date`, by `snapshot_time` or
`lead_days`), and, once the rain has been measured, how far off each forecast was.

**What it can't tell us:**

- It starts on Nov 11, 2026. Older forecasts aren't available.
- It only sees the forecast as our hourly runs fetched it. An NWS update that was
  replaced within the hour would be missed.
- NWS's issue time also changes when only other parts of the forecast change
  (temperature, wind), so several rows in a row can have the same rain numbers.
- There are usually several rows for the same `target_date` and `lead_days`
  (several updates on the same day). Pick one (for example the latest) before
  comparing.
- A same-day forecast (`lead_days` 0) can include hours that had already passed
  when NWS issued it.
- Forecast days follow Pacific local time (with daylight saving time), while the
  gauges' days are PST all year, so from March to November they're an hour apart.
- Gauge outages can appear as zero rainfall, which would make a forecast look
  wrong when the gauge was the problem.

### `data/forecast_history_recent.csv`: the last few weeks

The same columns, but only rows whose `target_date` is 21 days ago or later,
sorted by `target_date` then `snapshot_time`. It is rebuilt from the full
history file every run (and only rewritten when its contents change), so it
should never be edited by hand. It stays small (a few hundred KB) for the
dashboard to read. If it can't be written, the problem is logged and the run
carries on.

## Trail conditions (on the dashboard)

A rough running and biking guide for **one trail region at a time**, from the
average of that region's gauges (`TRAIL.REGIONS` in the `TRAIL` block). The
region is picked on the trail conditions map (click or tap a region, or Tab to it
and press Enter) or with the Region dropdown next to the heading; the default is
Peninsula foothills (Rancho San Antonio, Stevens Creek Reservoir, Trappers Trail,
Westwind), and the choice is remembered in the browser. (Before this, the tiles
averaged 7 gauges, `TRAIL.GAUGES`: Mountain View, Rancho San Antonio, Stevens
Creek Reservoir, Sunnyvale Hamilton, Palo Alto, Maryknoll Fields and Trappers
Trail.) The Valley floor is paved, so it is rated on the weather in each block and
recent rain instead of ground wetness (see the map section below). The tiles cover today
(two cards) and the next 2 days (a table), in three time blocks: morning 4-10 AM,
mid day 10 AM-4 PM and afternoon 4-10 PM (Pacific time). Ratings, best to worst:
Great, Good, Fair, Poor. Each block gets the **worse** of three checks:

- **Ground wetness:** rain in the 24 hours before the block + 1/2 of the rain
  24-48 hours before + 1/4 of the rain 48-72 hours before. Measured hourly rain
  is used up to the latest measured hour, then the forecast (each 6-hour forecast
  period spread evenly). Only gauges with data for all 72 hours are averaged.
  Running: under 0.40 in great, under 1.50 good, under 3.00 fair, otherwise
  poor. Biking: under 0.02, 0.05 and 0.15. The running limits (they were 0.10,
  0.50 and 1.00) were set from two runs at Rancho San Antonio: Sun Nov 15, 2026
  (wetness 0.24-0.39 in, felt very good) and Fri Nov 20, 2026 (1.36-1.95 in,
  felt good to very good). They are a first pass: early-season soils drain faster
  than saturated late-season soils, so recheck them later in the winter.
  **Season adjustment:** early in the water year the soil is dry and absorbs the
  first storms, so the same wetness gives less mud than in mid-winter. The limits
  are multiplied according to the season rain total, the average across those
  gauges of rain since Oct 1 (the dashboard's "Season to date" numbers, from the hourly
  files). Starting values: under 2.0 in, biking x 2.5; 2.0 to under 5.0 in,
  biking x 1.5; 5.0 in or more, x 1.0 (the base limits). Running is x 1.0 at
  every step for now. If the season total can't be determined, the multiplier is
  1.0. When a sport is adjusted, a line under the Trail conditions section says
  so. The steps are `SEASON_ADJUST` in the same `TRAIL` block.
- **Rain during the block** (forecast): under 0.01 in great; 0.01-0.15 good for
    running, fair for biking; more than 0.15 poor.
- **Comfort**, from the forecast feels-like temperature (`apparent_temp_f`): running
  great 45-68°F, good 38-45/68-78, fair 30-38/78-88, poor outside; biking
  great 50-72°F, good 42-50/72-82, fair 34-42/82-92, poor outside. At an exact
  boundary the better rating applies.

Under each of today's two tiles, a thin bar from Great to Poor has a marker
showing where within its rating the block sits (its label reads, for example,
"Good, close to Great"); the math is `ratingPosition` in `index.html`. When the
ground-wetness check alone is Good or worse, the Running tile adds a "watch for"
line (`TRAIL.WATCH_FOR`); never for biking or the Valley floor.

Values are compared as shown on the page (inches to 2 decimals, whole degrees).
Today's big rating is the worst of the blocks still ahead; if all have ended,
the afternoon block's ground wetness alone. If the forecast is missing or out of
date, only the ground wetness measured so far is used; if `apparent_temp_f`
isn't there yet, the comfort check is skipped. **All the numbers live in one
commented block, `TRAIL`, near the top of the trail-conditions part of the
script in `index.html`**, so they can be tuned in one place.

## Trail conditions map (on the dashboard)

A map above the Trail conditions section shows today's rating for each trail
region (Peninsula foothills, Skyline ridge, South Valley, Valley floor), drawn
with the real park and city shapes from `data/parks.geojson` and
`data/cities.geojson` (see "Boundary data" below). Each region is rated with its
own gauges (`TRAIL.REGIONS` in `index.html`), the same way as the Trail conditions
tiles show it when that region is selected; the Valley floor is paved, so it is rated on the weather in
each block (forecast rain and feels-like) instead of ground wetness, plus road
wetness, from the rain before each block (measured, then forecast, averaged over
the Valley floor gauges): 0.02 in or more in the 12 hours before the end of the
block, the block is at best Fair; 0.15 in or more in the 6 hours before its end,
Poor (`TRAIL.ROAD_WET` in
`index.html`). A Running / Biking switch recolors the map, and hovering over
or tapping a region (or Tab, then Enter) opens a card with its parks or cities,
the rating and its reason, the three time blocks, ground wetness and the number
of gauges. Clicking or tapping a region (or its label) also selects it for the
Trail conditions tiles; the selected region has a thick dark outline and a
check mark in its label. Each label is a pill in the rating's colors, placed in
open space near its region and joined to it by a thin line. The two shape files add about 51 KB (about 13 KB compressed); if they
can't be loaded, a short message replaces the map and the rest of the page works.

## Boundary data

Park outlines and city areas for the planned trail-region map. These are static
files: nothing updates them automatically, and the page doesn't use them yet.

- **`data/regions.json`**: the four regions (`foothills` Peninsula foothills,
  `skyline` Skyline ridge, `south` South Valley, `valley` Valley floor) and the
  parks and cities in each, one line per park or city. To move a park to another
  region, change its `"region"` and rebuild.
- **`data/parks.geojson`**: one feature per park, with properties `name`,
  `region` and `source` (the OpenStreetMap objects it came from). A park in
  several separate pieces is one feature with several polygons.
- **`data/cities.geojson`**: the Valley floor cities, same properties. These are
  **not** the full city limits: each city keeps only its flat part, east of a
  dividing road (the bay side), with every park in `parks.geojson` cut out, and
  leftover slivers under 5 hectares dropped. Palo Alto, Mountain View, Sunnyvale
  and Los Altos are cut along **Interstate 280**. **Cupertino** is cut along
  **Foothill Boulevard** instead, because most of the city lies south-west of
  I-280; north and south of the boulevard's ends the cut continues straight north
  and south. Each city's road is its `"east_of"` in `regions.json`; the roads are
  defined in `CUT_LINES` in `tools/build_boundaries.py`.
- **`tools/boundaries_report.txt`**: the report from the last build (what was
  matched, area, pieces, nearest gauge, full and trimmed city areas, overlaps).
- **`tools/midpen_comparison.txt`**: the check of the Midpen preserve outlines
  against Midpen's official boundaries (areas and overlap).

Shapes are simplified to about 20 m and coordinates rounded to 5 decimals (about 1 m).

**Midpen preserves in OpenStreetMap** are mapped as an open part plus separately
named closed areas (for example "Russian Ridge Open Space Preserve (closed area)",
and one unnamed area for Monte Bello). The `"also"` list on a park's line in
`regions.json` merges those pieces in. The results were checked against Midpen's
official "Preserve Boundary" layer; that layer comes with a warranty disclaimer
but no license allowing republication, so it is used only for that check, not
published here.

**Sources, licenses and the attribution to show:**

- **Parks, the I-280 line and the park cut-outs in the city areas: OpenStreetMap**,
  through the Overpass API. License:
  [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
  Show: **"© OpenStreetMap contributors"**, linked to
  https://www.openstreetmap.org/copyright (the dashboard's map already shows this
  for its tiles). The ODbL is share-alike: `data/parks.geojson` and
  `data/cities.geojson` are derived databases, so they stay under the ODbL, and
  anyone may reuse them on the same terms.
- **City limits: US Census Bureau**, 2025 cartographic boundary file for
  California places (`cb_2025_06_place_500k`, www2.census.gov). Public domain (a
  work of the US government); no attribution is required. Suggested credit:
  "City limits: U.S. Census Bureau, 2025 Cartographic Boundary Files."
- **Foothill Boulevard line (Cupertino's cut): US Census Bureau**, 2025 TIGER/Line
  roads for Santa Clara County (`tl_2025_06085_roads`, "N Foothill Blvd" and
  "S Foothill Blvd"). Public domain, like the city limits. (Overpass kept timing
  out on this road, and the Census file can be downloaded from anywhere.)

**Rebuilding** (by hand, never in the hourly workflow):

    pip install shapely pyproj pyshp
    python tools/build_boundaries.py              # parks, then the trimmed cities
    python tools/build_boundaries.py --untrimmed  # full city limits instead of the trimmed ones
    python -m unittest test_boundaries

The script makes one Overpass request for the parks and one for I-280 (trying two
other public Overpass servers if the main one fails), and downloads two Census
files (city limits, and the county's roads for Foothill Boulevard; `--cache-dir DIR` keeps the downloads and reuses them).
If the park download fails, the cities are trimmed with the park outlines already saved. Parks are found by
the names under `"match"` in `data/regions.json`; to fix a wrong match, add
`"osm": "relation/123"` (or `way/123`) to that park's line, and list extra pieces
under `"also"`.

The Overpass API can't be reached from the cloud environment these files were
made in, so they were built on GitHub's machines. The workflow that did it is kept
as **`tools/build-boundaries.workflow.yml`** (GitHub ignores it there, so it never
runs). To rebuild that way: on a new branch, copy it to
`.github/workflows/build-boundaries.yml`, change `add-boundaries` in its
`branches:` line to the new branch's name, and push a change to the build script
or `regions.json`. It builds the files, commits them and the report to that
branch, and runs the tests. Delete the workflow file again before merging.

## Visit counts

The page loads GoatCounter's script (`gc.zgo.at/count.js`), which counts page
views without cookies or personal data. The counts are at
https://fede-trails.goatcounter.com (only the owner can see them). To leave out
your own visits, open the site once in each browser you use with
`#toggle-goatcounter` added to the address, for example
`https://fdesilvaleon.github.io/rainfall-dashboard/#toggle-goatcounter`.

## Attribution and disclaimer

Data: Santa Clara Valley Water ALERT system
([alert.valleywater.org](https://alert.valleywater.org/)). Readings are
preliminary and unreviewed, may contain errors or gaps, and may be revised by
the district. This is an independent personal project and is not affiliated
with or endorsed by Santa Clara Valley Water. Data is provided as is, with no
guarantee of accuracy, completeness or timeliness, and the author is not
responsible for any decisions made from it. Map data and map tiles ©
[OpenStreetMap contributors](https://www.openstreetmap.org/copyright).
