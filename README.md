# Mountain View Rainfall Dashboard

This project collects rainfall readings for the Mountain View area from the
[Santa Clara Valley Water District's ALERT rain gauges](https://valleywateralert.org/scvwd/pgi.php).
A dashboard page will be added later and hosted free on GitHub Pages.

## How it works

- **`gauges.csv`** is the list of rain gauges to collect. To add a gauge, add a
  line with its ID number and name (the ID is in the gauge's link on the
  district's site).
- **`collect.py`** downloads each gauge's report page once. The page lists
  readings every 5 minutes; the collector keeps one row per hour and saves it
  to **`data/<gauge id>.csv`**. New hours are added to the history, and hours it
  already has are not saved twice.
- **`test_collect.py`** checks the collector works (run `python -m unittest`).
- **GitHub Actions** runs the collector automatically every 6 hours (about 6 am, noon,
  6 pm and midnight Pacific time) and saves any
  new data back to this repository. You can also run it any time from the
  **Actions** tab ("Collect rainfall data" → "Run workflow"), for example
  during a storm. Each run reads the last 30 days, so no readings are lost
  between runs.

## About the data

Each file has one row per hour, with three columns:

- `hour_pst`: the start of the hour, in Pacific Standard Time as reported by the
  district (for example `08:00` covers 08:00 to 08:59).
- `season_total_in`: the **running total for the rain season** in inches, taken
  from the last reading in that hour, exactly as reported.
- `rain_in_hour`: rain in that hour (this hour's season total minus the
  previous hour's). It is blank if either value is missing, or if the total went
  down (for example when a new season starts).

Other details:

- A reading of "----" means the gauge reported nothing. The collector uses the
  last real reading in the hour. If there isn't one, the season total is blank.
- The most recent hour is updated on later runs as the hour fills in.
- The data is preliminary and may contain errors, as noted on the district's site.
