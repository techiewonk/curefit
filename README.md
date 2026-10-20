# Curefit admin

A local Django admin for pulling Curefit/Kronos reports for many centers, keeping every run, and
looking at the results. It runs on your PC only (no password page: the only login is the emailed
Kronos OTP).

Built on **Django 6.1** with **django-unfold** (admin theme and UI components), **django-q2**
(job queue on the same SQLite database, no Redis), **django-constance** (settings you edit in the
admin) and **django-import-export** (CSV/XLSX exports).

## Quick start (Windows)

```
install.bat      once: virtual environment, packages, database
run.bat          starts the web server + job runner and opens http://localhost:8000
dev.bat          same, but the web server restarts when a .py file changes
```

Or by hand: `python manage.py serve` (`--reload` for development, `--port`, `--no-open`).
The job runner is its own process, so a running scrape keeps going when the web server restarts.
If the whole thing is stopped mid-scrape, the run shows as *interrupted* and can be continued.

## Using it

| Page | What it does |
|---|---|
| **Home** | Needs-attention list (failures, slow sections, unfinished sessions, data checks), latest totals, recent sessions |
| **Run a scrape** | Kronos login (send OTP, enter the code), period, center list, reports, speed; live estimate; progress, Pause, Stop. It offers to **continue** an unfinished session for the same period |
| **Live monitor** | Centers × reports grid that updates while a run is active (click a square to open its result) |
| **Analytics / Compare sessions** | Charts and per-center numbers for any session; difference between two sessions |
| **Sessions, Section results, Sales / Perf / Gym / Other cards** | Standard admin lists with search, filters and CSV/XLSX export |
| **Update from Kronos / Centers / Center lists / Known empty** | Refresh the center list; build the *actual* and *test* lists; centers that are always empty for a report |
| **Settings** | Delays, retries, timeouts, row cap, safety limits, the login email |

Speeds: Gentle (1 center at a time), Normal (2), Fast (4), Adaptive (starts at 2, halves when
Curefit's servers struggle, creeps up while answers are fast).

## What a run leaves behind

Database tables (SQLite `db.sqlite3`) and files under `runs/<date>/<period>_<id>/`:

```
session.json            status, period, centers, reports
centers/<id>_<name>/    one CSV per report for that center
merged/                 one CSV per report with every center, plus all_reports.xlsx
sales_report.csv perf_long.csv perf_pivot.csv gym_members.csv analysis.csv coverage.csv dashboard.html
```

A new run always fetches everything; only a *continued* session skips what it already saved.
Empty results are recorded (and a center that is empty in two sessions becomes *known empty* and
is skipped next time unless you ask to re-check). Failed and timed-out sections are retried on
continue.

## Code layout

```
config/    settings (Unfold sidebar, constance defaults), urls, local auto-login middleware
centers/   Center, CenterList, CenterSyncLog, KnownEmpty; sync from Kronos; admin
scraper/   models, kronos (HTTP), login, engine (the scrape), throttle, storage, files, analysis,
           checks, selectors (read queries), jobs + tasks (django-q2), resources (exports), admin
console/   the custom pages: forms, class-based views, templates (Unfold components + HTMX)
```

## Tests

```
python manage.py test        # 70+ tests, no network needed
```

## Notes

- `session.json` holds the Kronos login and `.django_secret` the Django key: both are git-ignored.
- The server only accepts `localhost` / `127.0.0.1` and binds to 127.0.0.1. Do not expose it:
  anyone who can reach the port can use the admin.
- Old reports from the earlier scripts are in `legacy_reports/`; their data was imported into the
  database. The pre-migration database is kept as `curefit.db.pre-django`.
