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

`install.bat` is safe to run again after an update. `run.bat` and `dev.bat` run it for you the first
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
  empty* and is skipped next time (tick "re-check known-empty" to ask anyway).
- Failed and timed-out sections are retried when you continue the session.
- After each run, automatic checks flag ACTIVE centers with no sales, big changes since the last run
  of the same period, and results that hit the row cap. They show up on Home.

## What to expect from Kronos (seen in real runs)

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
