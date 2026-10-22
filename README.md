# Curefit admin

A local Django admin for pulling Curefit/Kronos (Metabase) reports for many centers, keeping every run, and
looking at the results. It runs on your PC only: there is no password page, the only login is the
emailed Kronos OTP.

Built on **Django 6.1** with **django-unfold** (admin theme and UI components), **django-q2** (job
queue on the same SQLite database, no Redis), **django-constance** (settings you edit in the admin)
and **django-import-export** (CSV/XLSX exports). Needs Python 3.12 or newer.

## Setup and start (Windows)

```
install.bat          setup: checks Python, creates .venv, installs packages, runs migrations, system check
run.bat              starts the admin and opens http://localhost:8000
dev.bat              same, but the web server restarts whenever a .py file changes
update_packages.bat  called by the three files above; installs packages only when they changed
```

What `run.bat` / `dev.bat` do on every start, in this order:

1. If there is no `.venv` yet, run `install.bat` first.
2. **Packages:** if `requirements.txt` or `constraints.txt` changed since the last install (a hash is kept in
   `.venv\requirements.stamp`), run `pip install -r requirements.txt -c constraints.txt`. `constraints.txt`
   pins the exact version of every package. If this fails, check the internet connection and run `install.bat`.
3. **Database:** apply pending migrations, then collect the static files.
4. **`manage.py serve`:** end a job runner left over from an earlier start (`runner.pid`), start the job runner
   and the web server, fill the default center groups (`seed_groups`), open the browser, and then, in the
   background, run the catch-up steps `link_customers`, `merge_masters` and `merge_months` (they fill the
   customers, the all-runs tables and the monthly table from older sessions). A catch-up step that fails is
   printed and never stops the start; you can run it again by hand later.

With `dev.bat`'s auto-reload, migrations and catch-up steps run once, at the start: a restart caused by a code change
does not run them again (stop and start `dev.bat` after adding a migration). `dev.bat` also turns on Django's debug
pages (`CUREFIT_DEBUG=1`).

By hand: `python manage.py serve` (options: `--reload`, `--port 9000`, `--no-open`, `--no-jobs`).

### Two processes, one command

`serve` starts the **web server** (the pages) and the **job runner** (django-q2's `qcluster`, which
does the scraping). They are separate so that a scrape that takes hours keeps going when the web
server restarts (auto-reload, a crash) and the pages stay responsive meanwhile. Stopping the command, or
simply closing its window, stops both; a scrape that was in flight shows as *interrupted* and can be continued.
If the job runner dies on its own, the window prints a warning: scrapes and automatic runs do not start until you
restart `run.bat`. On the way out, `serve` folds the SQLite write-ahead log back into `db.sqlite3`.

## First run

1. Open **Operate → Run a scrape**, click *Send OTP*, enter the code from the email, *Verify*.
2. Open **Centers → Update** and click *Update from Kronos*: this loads every center.
3. On **Centers → Centers**, tick centers and use *Add to Actual list* / *Add to Test list*.
4. On **Run a scrape**, pick the *test* list first, a period and the reports, then *Start*.

## The sidebar

| Section | Pages | What they are for |
|---|---|---|
| **Performance** | Dashboard, Revenue by group, Monthly metrics, Analysis and compare, Other cards | Management view of the Franchise Performance numbers: revenue by group and month (charts on the Dashboard, the sheet-like table on *Revenue by group*), one row per gym and month with every metric (*Monthly metrics*), month vs month / year / range / group comparisons (*Analysis and compare*), and the rows of the other dashboard cards. Click a gym to see all its metrics over time. |
| **Gym members** | All, Per run / session, Leads | *My Gym Member*: the merged all-runs table (one row per member pack), the raw rows of each run, and *Leads* (one row per customer with a lead label). |
| **Expired packs (sales)** | All, Per run / session, Day coverage, Missing days | *Expired Pack User Details*: the merged all-runs table, the raw rows of each run, which gym and day is stored, and a page that fetches the days still missing. |
| **Operate** | Status, Run a scrape, Live monitor | Start, watch and stop scrapes. *Status* lists what needs attention (failures, unfinished sessions, skipped automatic runs, data checks) and the Kronos login state. |
| **Results** | Session analytics, Compare sessions, Sessions, Section results | One session's charts and per-center numbers, the difference between two sessions, the list of every run, and one row per center × report of every run (status, rows, time). |
| **Centers** | Update, Centers, Center groups, Center lists, Known empty, Sync history | Refresh the center list from Kronos; groups (with monthly targets and notes per gym); the *actual* and *test* lists; centers that are always empty for a report; past center updates. |
| **Automation** | Automatic runs, Schedules | Campaigns that start scrapes by themselves, and django-q2's schedules behind them (read them, do not edit them: they follow the campaign). |
| **System** | Jobs, Queued tasks, Failed tasks, Settings | One job per scrape with its state, progress and error text; django-q2's queue and failures; the editable settings. |

Every admin list has range filters (numbers, dates, date-times), text filters, value check-boxes and dropdowns; press
*Filter* to apply. **Export** on any list downloads exactly the filtered rows as CSV or XLSX. Analytics, Compare and
Revenue by group have their own *Download CSV / XLSX* links. Scraped rows cannot be edited or deleted by hand.

## Runs (sessions)

A **run** (a *session*) is one scrape: a period, a set of centers and a set of reports. It leaves a row per center and
report under *Section results* (ok, empty, failed, skipped, slow) and the rows themselves under the report's
*Per run / session* page.

- **Run a scrape** takes the period, a center list, groups or single gyms, the reports, the speed, the split
  (day / week / month, see below) and, for sales, how to ask for it. The estimate next to *Start* uses the timings
  of your earlier runs. One scrape runs at a time. A run longer than 400 days (120 when sales are asked one day at
  a time) is refused: split it.
- **Speed:** Gentle (1 center at a time), Normal (2), Fast (4), or Adaptive (starts at 2, halves when
  Curefit's servers struggle, creeps back up while answers are fast; never above the *Max workers*
  setting, 1-8). Be considerate: this is Curefit's production reporting system.
- **Continue:** if an unfinished session exists for the same period, the Run page offers to continue it; it
  fetches only what is missing and retries failed and timed-out sections. *Also ask again for empty results* and
  *re-check known-empty* make it ask for those too.
- **Stopping:** *Stop* (Run page, Status, or the Sessions/Jobs actions) ends the run cleanly after the requests
  in flight; a run that is still queued is cancelled. If a Metabase request is stuck, *Stop now* ends the job
  runner's worker at once; the session is marked *interrupted*. Everything already saved is kept, and
  **Continue** finishes it later. The job says *Stopped by you* so you can tell it apart from a finished run.
- **Statuses:** a session is *running*, *complete*, *incomplete* (something is missing: continue it) or
  *interrupted* (the runner stopped under it). A job is *queued*, *running*, *done*, *error*, *interrupted* or
  *cancelled*; its error text says why. *Done* with a message such as "a step after the scrape failed" means the
  data was saved but merging or the checks did not finish: the message names the step.
- **Section results → Retry selected** queues the chosen sections again (not while another scrape runs). *Mark
  selected as known empty* only takes rows whose status is *empty*.
- A new run always fetches everything; only a *continued* session skips what it already saved.
- **Known empty:** a center that is empty for a report in two sessions becomes *known empty* and is skipped next
  time. This only counts when the report returned data for other centers in both sessions, so an old period that
  is empty for everyone never marks anyone.
- **Checks:** after each run, automatic checks flag ACTIVE centers with no sales, big changes since the last run
  of the same period, and results that hit the row cap. They show up on Status.

### Daily, weekly, monthly numbers (Split by)

The Run page's **Split by** setting (day, week or month) is sent to the Franchise Performance cards, so every
metric (Total Revenue, new/repeat split, ...) is stored as one number per day, week or month. The sales cards are
not split (they list individual sales). Sessions for the same period but different splits are kept apart.

**Monthly metrics** turns these into one row per gym and month. When several sessions cover a month, the month
uses one split only (the one covering most days of the month, then the newest session), so day and week rows of the
same month are never added together and a re-run corrects a month instead of adding to it. A week that crosses a
month end is shared out by days and marked *estimated*. Rates (names with rate, %, retention, ...) are averaged across
gyms, everything else is added up; *Settings → Metrics* lets you name extra rates (`RATE_METRICS`) or metrics that
must always be added up (`NOT_RATE`).

**Partial months:** a month that is not fully covered (the current month, or a run that stopped half-way) is marked
with `*` and "N of M days". It is shown but left out of averages, rankings and month-on-month growth.

## Automatic runs (campaigns)

*Automation → Automatic runs* holds campaigns that start a scrape by themselves. Four ready-made ones are created
switched off: **Daily report** (yesterday), **Weekly report** (last Monday–Sunday) and **Monthly report** (the
previous calendar month) on the actual list, and a **Daily test** on the test list.

A campaign has: when to run (every day, a weekday, a day of the month, or a **cron expression** such as
`30 6 * * 1-5`, local time); which period (the last N days up to yesterday, last week, last month, month to date,
fixed dates, or *missing sales days* of the 90-day window); which gyms (a center list, plus groups, plus single
gyms); and the same report, speed, split and sales options as the Run page. With *continue unfinished* on, the
next run for the same period continues the last unfinished session instead of starting over.

Switch one on and the job runner starts it at the time set. *Run now* on a row tests it with the period it would
report on (a real scrape; it asks for confirmation). The *Last result* column shows what happened:

- **started**, then **finished** / **incomplete** / **failed** when its job ends;
- **waiting**: another scrape was running; it starts by itself when that one ends;
- **skipped**: switched off, not logged in to Kronos (the OTP is manual, so log in and run it again), or its last
  start is still going;
- **failed**: no gyms picked, or the run could not be queued (the message says why).

The job runner must be running at that time (`run.bat` does that). A run missed while the PC was off fires when
the runner starts again. Deleting a campaign also removes its schedule.

Prefer the operating system's scheduler? This does the whole scrape in one process and needs no job runner:

    .venv\Scripts\python manage.py auto_run --list
    .venv\Scripts\python manage.py auto_run "Daily report (actual list)" --wait
    schtasks /Create /SC DAILY /ST 06:30 /TN "Curefit daily" /TR "\"D:\path\.venv\Scripts\python.exe\" \"D:\path\manage.py\" auto_run \"Daily report (actual list)\" --wait"
    30 6 * * *  cd /path && .venv/bin/python manage.py auto_run "Daily report (actual list)" --wait    # crontab

Exit code 0 = complete, 1 = incomplete or failed, 2 = skipped.

## Groups and Revenue by group

**Groups** (GYMS, Gym Hybrid, ...) are yours to edit under *Centers → Center groups*: create, rename or delete a
group, and add or remove gyms (type to search). On the *Centers* list, select centers and use the
*Add to group: X* / *Remove from group: X* actions. Set each gym's **monthly target** and **note** in the same list.
A gym can be in several groups: it then counts in each of them, so the group rows can add up to more than the
distinct total (shown separately, every gym once).

The three groups of the management sheet (GYMS, HYBRIDS, Gym Hybrid) are created by a migration, and the first
center sync (or `python manage.py seed_groups`, also run at start-up) puts the sheet's gyms into them by name. Only
**empty** groups are filled, so gyms you add or remove, and groups you delete, are never put back. A name that matches
two centers (for example *Cult Gym Indiranagar*) is listed and left for you to add by hand. For another sheet:

    python manage.py import_groups 20oct.xls            # preview
    python manage.py import_groups 20oct.xls --apply    # one group per section of the sheet

**Upload a group's gyms from a CSV** on the group's own form (*Center groups → Add* or open a group): pick a CSV with
the center IDs in a column headed `ID` (a `Name` column beside it is only for you; *Download a sample CSV* is linked
under the field), then choose **Add to the gyms below** or **Replace the gyms** and save. Gyms are matched by ID, so
names never need to match. An ID that matches no center stops the save and is listed; text lines (a total, a note)
are skipped and reported. One file per group.

**Revenue by group** shows gyms × months of the Franchise Performance **Total Revenue** (run it with
*Split by: month*): the monthly figures, average, rank for the latest full month, rank by average, change vs the
previous month, monthly target and % achieved, with group totals at the top, then one table per group like the
sheet (rankings within the group, Indian digit grouping, a Note column). Partial months are marked and kept out of
averages and ranks. A *From / To* range with no stored months shows "no months in this range" and where the data
starts. Download CSV/XLSX in the same layout.

## Merged tables (all runs) and customers

Each run keeps its own rows (the raw record), and every finished run is also **merged** into two all-runs tables, so
repeated and overlapping runs never double count:

| Table | One row per | Updated when a run sees it again |
|---|---|---|
| **Gym members → All** (My Gym Member) | member + pack + pack start date | status, class level, amount and so on come from the **newest scrape**; pack end date and last class date only ever move **forward** (an extension updates the row, it does not add one) |
| **Expired packs → All** (Expired Pack User Details) | member + pack + pack end date | renewed / active / revenue come from the newest scrape; a renewal has a new end date, so it is a new row |

Rows are never deleted by a run; a pack that no longer shows up keeps its *last seen* time. The newest scrape
wins by its fetch time, so merging an old session late cannot undo newer data. The merged and derived tables cannot
be deleted by hand; deleting a session or a center removes its own rows. Fill the tables from sessions scraped
earlier with `python manage.py merge_masters` (also done at start-up).

**Keep run rows** (Settings → Storage) can drop the per-run sales/gym rows of older sessions once they are merged
(performance numbers are kept). Such a session is marked *rows pruned*: Analytics and Compare say its run rows were
removed, its sales figures there are incomplete, and the checks do not compare against it. The data
is still in the all-runs tables.

**Customers (leads):** *Gym members → Leads* has one row per Kronos user id (phone numbers are the fallback and
may be several per person), with the newest pack, end date and a lead label: active, expiring soon, expired
recently, lapsed. Thresholds are in Settings → Leads. Link old sessions with `python manage.py link_customers`.

**One-time repair after an update:** when an update changes how stored figures are computed, `scraper/repairs.py`
recomputes customers, the all-runs tables and the monthly table once (before any pruning) and remembers it in a
*RepairLog* row, so it never runs twice.

## Missing days (Expired Pack User Details by day)

This card has no split of its own and only holds the **last 90 days before today**; its start/end filter on the
pack end date. On the Run page, *Ask for sales by* a **day** or a **week** (default in Settings) asks for one piece
at a time, and *only fetch days not stored yet* fetches just the gaps. *Expired packs → Missing days* shows every
day of the 90-day window with how many gyms of a list are stored, and buttons that fetch one day, one gym or every
missing day. *Day coverage* lists every stored gym and day. Automatic runs have the same options, plus a *missing
sales days* period.

## When the Kronos login expires

The Kronos login (from the OTP) lasts a limited time. Status and the Run page show whether you are logged in.

- **Before a run:** a scrape, *Run now* or a fetch from Missing days is refused with "Not logged in to Kronos";
  an automatic run is *skipped* with that reason on Status.
- **During a run:** after 3 answers in a row that reject the login (401/403; *Settings → Auth fail limit*) the run
  stops. The job ends with the error "Kronos login rejected 3 times - log in on the Run page and press Continue".
  Log in on the Run page (*Send OTP*, *Verify*) and **Continue** the session: nothing already saved is fetched again.

## What a run leaves behind

Database tables (SQLite `db.sqlite3`) and files under `runs/<date>/<period>_<id>/`:

```
session.json            status, period, centers, reports
centers/<id>_<name>/    one CSV per report for that center
merged/                 one CSV per report with every center, plus all_reports.xlsx
sales_report.csv perf_long.csv perf_pivot.csv gym_members.csv analysis.csv coverage.csv dashboard.html
```

Logs go to the console window and to `logs/curefit.log` (5 files of 5 MB). Login tokens are removed from every line.

## What to expect from Kronos (seen in real runs)

- **The sales report only holds the last 90 days before today** ("Expired Pack User Details - Upto
  Last 90 Days"). For a period that ends earlier it comes back empty for every center, and asking costs
  about a minute per center. The Run page warns about this; untick *sales* for old periods.
- The *Sales* and *Total Revenue* cards answer in seconds; the other perf-dashboard cards often take
  **minutes per center**, and an empty answer can take 5–8 minutes. Plan long runs accordingly.
- `sales_center_walkins` and `sales_cm_walkins` currently fail with an HTTP 500 from Metabase for
  every center; after three failures in a row the run skips that report.
- Many centers return no rows for some reports (inactive or new centers, centers missing from a
  dashboard). That is normal; *Known empty* stops them costing time.

## Commands

| Command | What it does |
|---|---|
| `serve [--reload] [--port N] [--no-open] [--no-jobs]` | web server and job runner (what `run.bat` / `dev.bat` call) |
| `run_scrape --start D --end D --list test [--only sales,perf] [--speed S] [--granularity G] [--sales-chunk C]` | one scrape from the command line, as a normal job; `--session ID` continues one. Exit 0 = complete, 1 = incomplete or failed |
| `auto_run NAME [--wait]` / `auto_run --list` | start a campaign (see Automatic runs) |
| `dbbackup [--dir D] [--keep N]` | safe copy of the database into `backups/db_YYYYMMDD.sqlite3`, also while the admin runs |
| `merge_masters` | fill the all-runs tables from every session |
| `merge_months` | rebuild the monthly metrics table |
| `link_customers` | tie sales / gym rows of older sessions to customers |
| `seed_groups` | fill the empty management-sheet groups by gym name |
| `import_groups FILE [--apply]` | groups from a management sheet (.xls) |

Run them as `.venv\Scripts\python manage.py <command>` in the project folder.

## Backups

`db.sqlite3` holds all data. SQLite keeps recent changes in `db.sqlite3-wal` for a while, so a plain copy of
`db.sqlite3` alone, while the admin runs, can miss them. Use `python manage.py dbbackup` (safe while running), or
copy the file after stopping `run.bat`. To restore: stop the admin, replace `db.sqlite3` with the copy, and delete
`db.sqlite3-wal` and `db.sqlite3-shm`.

## Troubleshooting

- **A scrape stays *queued*, or a job failed with "Not picked up ... the job runner is not running":** the runner
  is not running (the window was closed, or it crashed: look for "the job runner stopped" in the window). A queued
  job that no runner takes within 10 minutes fails with that message. Close the window and start `run.bat` again, then queue the run again or continue the session.
- **A page shows an error right after an update:** stop `run.bat` (close its window) and start it again. That
  installs changed packages, applies migrations and restarts the job runner with the new code.
- **"Could not install the packages":** check the internet connection, then run `install.bat`.
- **"Refused: this admin only answers its own pages":** a link from another site or a file was opened. Type
  `http://localhost:8000` in the address bar instead.
- **A run ended with "Kronos login rejected":** see *When the Kronos login expires*.
- **A start-up step failed** (printed in the window): the admin works anyway; run that command by hand later.
- **Numbers look too low for a month:** look for the `*` partial-month marker, and for *rows pruned* on the session.
- **Logs:** `logs/curefit.log`.

## Code layout

```
config/    settings (Unfold sidebar, constance settings, logging), urls, local auto-login middleware
centers/   Center, CenterGroup, CenterList, CenterSyncLog, KnownEmpty; sync from Kronos; admin
scraper/   models, kronos (HTTP), login + auth, engine (the scrape), throttle, storage, files, analysis,
           checks, selectors (read queries), jobs + tasks (django-q2), autorun (campaigns), masters (all-runs
           tables), customers, perfwide + revenue (monthly figures), coverage (missing days), repairs, admin
console/   the custom pages: forms, class-based views, templates (Unfold components + HTMX), serve command
templates/ the Dashboard (admin index) and the action confirmation page
```

## Tests

No network is needed. **Never run the tests against the live database:** point both settings at throw-away files
(Git Bash):

```
CUREFIT_DB=nonexistent_me.sqlite3 CUREFIT_TEST_DB=test_me.sqlite3 ./.venv/Scripts/python.exe manage.py test
```

`CUREFIT_DB` must name a file that does not exist, so nothing can touch `db.sqlite3`; `CUREFIT_TEST_DB` is the file
the test runner creates and deletes. Remove `nonexistent_me.sqlite3*` and `test_me.sqlite3*` afterwards. Give each
parallel test run its own names. You can pass labels such as `scraper.test_jobs` to run part of the suite.

## Notes

- `session.json` holds the Kronos login and `.django_secret` the Django key: both are git-ignored.
- The server only accepts `localhost` / `127.0.0.1` and binds to 127.0.0.1, and it refuses requests started by
  another website. Do not expose it: anyone who can reach the port can use the admin.
- Settings (System → Settings) only accept safe values: retries 1-10, workers 1-8, timeouts 10-900 seconds,
  pauses 0-60 seconds.
- Everything from before the Django version (the old reports, the old databases) is in `old/`. Its data was
  imported into the current database; the folder is not part of the repository and can be deleted when you no longer
  need it.
