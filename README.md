# Curefit admin

A local Django admin for pulling Curefit/Kronos reports for many centers, keeping every run, and
looking at the results. It runs on your PC only: there is no password page, the only login is the
emailed Kronos OTP.

Built on **Django 6.1** with **django-unfold** (admin theme and UI components), **django-q2** (job
queue on the same SQLite database, no Redis), **django-constance** (settings you edit in the admin)
and **django-import-export** (CSV/XLSX exports). Needs Python 3.12 or newer.

## Setup and start (Windows)

```
install.bat    setup: checks Python, creates .venv, installs packages, runs migrations, system check
run.bat        starts the admin and opens http://localhost:8000
dev.bat        same, but the web server restarts whenever a .py file changes
```

`install.bat` is safe to run again after an update. Static files are collected at every start. `run.bat` and `dev.bat` run it for you the first
time and apply any pending migrations on every start, so after pulling new code you just start them.

By hand: `python manage.py serve` (options: `--reload`, `--port 9000`, `--no-open`, `--no-jobs`).

### Two processes, one command

`serve` starts the **web server** (the pages) and the **job runner** (django-q2's `qcluster`, which
does the scraping). They are separate so that a scrape that takes hours keeps going when the web
server restarts (auto-reload, a crash) and the pages stay responsive meanwhile. Stopping the command
stops both; a scrape that was in flight shows as *interrupted* and can be continued.

## First run

1. Open **Run a scrape**, click *Send OTP*, enter the code from the email, *Verify*.
2. Open **Update from Kronos** and click the button: this loads every center.
3. On **Centers**, tick centers and use *Add to Actual list* / *Add to Test list*.
4. On **Run a scrape**, pick the *test* list first, a period and the reports, then *Start*.

## Using it

| Page | What it does |
|---|---|
| **Home** | Needs-attention list (failures, slow sections, unfinished sessions, data checks), latest totals, recent sessions |
| **Run a scrape** | Kronos login, period, center list, reports, speed; live estimate; progress, Pause, Stop. Offers to **continue** an unfinished session for the same period |
| **Live monitor** | Centers × reports grid that updates while a run is active (click a square for its result) |
| **Analytics / Compare sessions** | Charts and per-center numbers for any session; the difference between two sessions |
| **Sessions, Section results, Sales / Perf / Gym / Other cards** | Standard admin lists with search, filters and CSV/XLSX export |
| **Update from Kronos / Centers / Center lists / Known empty** | Refresh the center list; build the *actual* and *test* lists; centers that are always empty for a report |
| **Settings** | Delays, retries, timeouts, row cap, safety limits, the Kronos login email |

**Speed:** Gentle (1 center at a time), Normal (2), Fast (4), or Adaptive (starts at 2, halves when
Curefit's servers struggle, creeps back up while answers are fast; never above the *Max workers*
setting). Be considerate: this is Curefit's production reporting system.

### Stopping a run

*Stop* (Run page, Home, or the Sessions/Jobs admin actions) ends the run cleanly after the requests in
flight, and a run that is still queued is simply cancelled. If a Metabase request is stuck, *Stop now*
ends the job runner's worker at once; the session is marked *interrupted* and **Continue** finishes it later.
Everything already saved is kept.

### Daily, weekly, monthly numbers

The Run page's **Split by** setting (day, week or month) is sent to the Franchise Performance cards, so every
metric (Total Revenue, new/repeat split, ...) is stored as one number per day, week or month in *Perf metrics*
(`metric_date` is a real date; filter it with a from/to range). Analytics draws the trend. The sales cards are not
split (they list individual sales). Sessions for the same period but different splits are kept apart.

### Filters and downloads

Every admin list has range filters (numbers, dates, date-times), text filters, value check-boxes and
dropdowns; press *Filter* to apply. **Export** on any list downloads exactly the filtered rows as CSV or XLSX.
Analytics and Compare have their own *Download CSV / XLSX* links.

### One complete table per report (all runs merged)

Each run keeps its own rows (the raw record), and every finished run is also **merged** into two tables under
*Data*, so repeated and overlapping runs never double count:

| Table | One row per | Updated when a run sees it again |
|---|---|---|
| **Gym members (all, merged)** — My Gym Member | member + pack + pack start date | status, class level, amount and so on come from the **newest scrape**; pack end date and last class date only ever move **forward** (an extension updates the row, it does not add one) |
| **Expired packs (all, merged)** — Expired Pack User Details | member + pack + pack end date | renewed / active / revenue come from the newest scrape; a renewal has a new end date, so it is a new row |

Rows are never deleted by a run; a pack that no longer shows up keeps its *last seen* time. The newest scrape
wins by its fetch time, so merging an old session late cannot undo newer data. Fill the tables from sessions
scraped earlier with `python manage.py merge_masters` (also done at start-up). To save space, **Keep run rows** in
Settings can drop the per-run sales/gym rows of older sessions once they are merged (performance numbers are kept).

**Expired Pack User Details by day or week.** This card has no split of its own and only holds the last 90 days
before today; its start/end filter on the pack end date. On the Run page, *Ask for sales by* a **day** or a **week**
(default in Settings) asks for one piece at a time, and *only fetch days not stored yet* fetches just the gaps.
*Results → Sales day coverage* lists every day of the 90-day window with how many centers are stored and links to
fill the gaps. Automatic runs have the same two options.

### Revenue by gym (management view)

*Results → Revenue by gym* shows gyms × months from the Franchise Performance **Total Revenue** (run it with
*Split by: month*): the monthly figures, average, rank for the latest full month, overall rank, change vs the
previous month, monthly target and % achieved, with group totals at the top, then **one table per group** like the sheet (Average, Ranking for the latest full month and Ranking by average, both within the group; Indian digit grouping; a Note column). The current month is shown but kept
out of averages and ranks. Download CSV/XLSX in the same layout. When several sessions cover a month, the newest
one is used, so re-running a month corrects it instead of adding to it.

**Groups** (GYMS, Gym Hybrid, ...) are yours to edit under *Centers → Center groups*: create, rename or delete a
group, and add or remove gyms (type to search). On the *Centers* list, select centers and use the
*Add to group: X* / *Remove from group: X* actions. A gym can be in several groups. Set each gym's
**monthly target** in the same list. To start from a management sheet:

    python manage.py import_groups 20oct.xls            # preview
    python manage.py import_groups 20oct.xls --apply    # one group per section of the sheet (GYMS, HYBRIDS, Gym Hybrid)

**Customers (leads):** *Data → Customers (leads)* has one row per Kronos user id (phone numbers are the
fallback and may be several per person), with the newest pack, end date and a lead label: active, expiring soon,
expired recently, lapsed. Thresholds are in Settings. Link old sessions with `python manage.py link_customers`.

### Automatic runs (schedules)

*Automation → Automatic runs* holds ready-made, switched-off runs: **Daily** (yesterday), **Weekly** (last
Monday–Sunday), **Monthly** (the previous calendar month) on the actual list, and a **Daily test** on the test
list. Switch one on and the job runner starts it at the time set (daily, weekly, monthly, or any **cron
expression** such as `30 6 * * 1-5`). Use *Run now* on a row to test it with the period it would report on.
A run is skipped, and the reason shown on Home, when Kronos is not logged in (the OTP is still manual), another
scrape is running, or it already started today. The job runner must be running (`run.bat` does that).

Prefer the operating system's scheduler? This does the whole scrape in one process and needs no job runner:

    .venv\Scripts\python manage.py auto_run --list
    .venv\Scripts\python manage.py auto_run "Daily report (actual list)" --wait
    schtasks /Create /SC DAILY /ST 06:30 /TN "Curefit daily" /TR "\"D:\path\.venv\Scripts\python.exe\" \"D:\path\manage.py\" auto_run \"Daily report (actual list)\" --wait"
    30 6 * * *  cd /path && .venv/bin/python manage.py auto_run "Daily report (actual list)" --wait    # crontab

Exit code 0 = finished, 1 = incomplete or failed, 2 = skipped.

## What a run leaves behind

Database tables (SQLite `db.sqlite3`) and files under `runs/<date>/<period>_<id>/`:

```
session.json            status, period, centers, reports
centers/<id>_<name>/    one CSV per report for that center
merged/                 one CSV per report with every center, plus all_reports.xlsx
sales_report.csv perf_long.csv perf_pivot.csv gym_members.csv analysis.csv coverage.csv dashboard.html
```

- A new run always fetches everything; only a *continued* session skips what it already saved.
- Empty results are recorded. A center that is empty for a report in two sessions becomes *known
  empty* and is skipped next time (tick "re-check known-empty" to ask anyway). This only counts when
  the report returned data for other centers in both sessions, so an old period that is empty for
  everyone never marks anyone.
- Failed and timed-out sections are retried when you continue the session.
- After each run, automatic checks flag ACTIVE centers with no sales, big changes since the last run
  of the same period, and results that hit the row cap. They show up on Home.

## What to expect from Kronos (seen in real runs)

- **The sales report only holds the last 90 days before today** ("Expired Pack User Details - Upto
  Last 90 Days"). For a period that ends earlier it comes back empty for every center, and asking costs
  about a minute per center. The Run page warns about this; untick *sales* for old periods.
  (Checked live: a period ending 97 days ago returned 0 rows, one ending 66 days ago returned 32.)
- The *Sales* and *Total Revenue* cards answer in seconds; the other perf-dashboard cards often take
  **minutes per center**, and an empty answer can take 5–8 minutes. Plan long runs accordingly: the
  estimate on the Run page uses the timings of your earlier runs.
- `sales_center_walkins` and `sales_cm_walkins` currently fail with an HTTP 500 from Metabase for
  every center; after three failures in a row the run skips that report.
- Many centers return no rows for some reports (inactive or new centers, centers missing from a
  dashboard). That is normal; use *Known empty* so they stop costing time.
- If the Kronos session expires mid-run, the run stops after three 401/403 answers. Log in again on
  the Run page and continue the session.

## Code layout

```
config/    settings (Unfold sidebar, constance defaults), urls, local auto-login middleware
centers/   Center, CenterList, CenterSyncLog, KnownEmpty; sync from Kronos; admin
scraper/   models, kronos (HTTP), login, engine (the scrape), throttle, storage, files, analysis,
           checks, selectors (read queries), jobs + tasks (django-q2), resources (exports), admin
console/   the custom pages: forms, class-based views, templates (Unfold components + HTMX), serve command
```

## Tests

```
python manage.py test        # 70+ tests, no network needed
```

## Notes

- `session.json` holds the Kronos login and `.django_secret` the Django key: both are git-ignored.
- The server only accepts `localhost` / `127.0.0.1` and binds to 127.0.0.1. Do not expose it:
  anyone who can reach the port can use the admin.
- `db.sqlite3` holds all data; back it up with a plain file copy while the app is stopped.
- Reports from the earlier scripts are in `legacy_reports/` (their data was imported into the
  database). The pre-migration database is kept as `curefit.db.pre-django`.
