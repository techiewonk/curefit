# Curefit Admin — feature plan

Scope: a local web app (this PC, one shared Kronos login). The only authentication is the
emailed OTP that already exists; the page itself needs no login.

## 1. What we learned from real runs (not mocks)

| Finding | Evidence |
|---|---|
| Real extraction works through the web app | Test list (centers 1, 7, 15), `perf` report, session `a32b94`: 3/3, complete |
| Scrapes survive a server restart | Worker is its own process; the server restarted (pid changed) mid-job and the job still finished |
| Center 1 is fast | sales 46 rows, perf 10, gym 3,293 — each 2–3 s |
| Centers 7 and 15 return **no perf rows**, repeatedly, even after a retry on a fresh token | Both are ACTIVE. Not transient. Needs a comparison against the Kronos screen |
| Some cards are very slow | Center 15 `gym`: still running after 17 min; center 15 `sales` took ~2 min; center 7 `gym` 57 s |
| Not every center is live | 1,437 centers known: 541 ACTIVE, 896 INACTIVE. 25 of the 59 earlier zero-row centers were INACTIVE |
| Full run time is the main risk | History: median 26–32 s, mean 47–56 s, p90 83–116 s per center/report. 541 centers × 18 reports ≈ 100+ h one by one |

## 2. Principles
1. Never overload Curefit: bounded concurrency, per-request delay, automatic back-off.
2. Every run is a session and is resumable. Nothing is lost when a run stops.
3. The panel shows what is true: empty vs failed vs slow vs not-yet-run are different states.
4. One job at a time; reads (browsing data) never block on a running job.

## 3. Admin panel — pages and features

### 3.1 Home
- Login state, running job (progress, ETA), last session health, alerts (failed / empty /
  slow / truncated), quick buttons: Run test, Continue last, Update centers.

### 3.2 Run
- **Centers:** Test list / Actual list / named lists / "all ACTIVE" / ad-hoc selection.
- **Period:** presets (last month, last 30 days, month to date) or custom dates.
- **Reports:** groups — Core (sales, perf, gym), Perf cards, Sales cards — or individual.
- **Speed profile:** Gentle (1) · Normal (2–3) · Fast (4) · Adaptive (auto back-off).
- **Estimate before start**, from measured timings of earlier sessions (per report), not a guess.
- **Controls:** Start · Pause · Stop · Continue · Retry failed only · Retry empty only.
- **Order of work:** phase A core reports for all centers first, then extra cards, or
  center-by-center (chosen per run).

### 3.3 Live monitor
- Matrix of centers × reports with colour states (waiting · running · ok · empty · failed ·
  skipped · slow), click a cell for its log and row count.
- Log stream, throughput, ETA, current concurrency, back-off events.

### 3.4 Sessions
- List with status, coverage, missing; details; downloads; re-merge; open folder.
- **Compare two sessions** (rows and revenue per center, what changed).
- Archive / delete a session (with confirmation, DB rows and folder together).
- Export a session as a zip.

### 3.5 Data browser
- One table per report across all centers: search, filter by center/cluster, sort, paginate,
  column picker, download CSV / XLSX.
- Member lookup by phone or user id (shows a privacy notice).

### 3.6 Analytics
- Existing charts, plus: month-over-month trend, center ranking, cluster roll-ups,
  new-vs-repeat, expiries/retention, funnels, ratings.

### 3.7 Centers
- Sync from Kronos (done), Test/Actual lists (done), named lists, cluster/city/type filters.
- **Change history** (added / removed / state changes / renames) kept per sync.
- **Known-empty registry:** centers that are consistently empty for a report are marked, so
  they stop counting as failures (and can be re-checked on demand).

### 3.8 Data quality
- Automatic checks after each session: row counts vs the previous session, totals reconcile
  with the perf report, duplicates, row-cap truncation, ACTIVE-but-empty centers.
- **Spot-check helper:** open the same center/period in Kronos to compare by eye.

### 3.9 Schedules
- Recurring runs (e.g. monthly on the 2nd, nightly "continue unfinished"). A scheduled run
  that needs a new OTP waits and shows a banner instead of failing silently.

### 3.10 Settings
- Login email, default speed profile, poll timeout, row cap, retention, paths, theme.

### 3.11 Logs & diagnostics
- Worker logs, structured events, health page (db size, disk, session age), diagnostics
  bundle for support (never contains tokens, cookies or the OTP).

### 3.12 Help
- Short in-app runbook: how to log in, test, run all, continue, merge, read coverage.

## 4. Pipeline and parallelism

Stages per session: **plan → fetch → validate → store → export/merge → report**.

| Runs serially | Runs in parallel |
|---|---|
| Kronos login / OTP, center sync | Centers (bounded by the speed profile, 1–4) |
| Sections of one center (they share one token) | — |
| Database writes (SQLite, short transactions) | — |
| Export / merge at the end of a session | — |
| One job at a time | Reads from the admin never wait for a job |

Protection for Curefit's servers:
- A global concurrency cap and a minimum delay between requests.
- **Adaptive back-off:** on HTTP 429 / 5xx or slow answers, halve concurrency, recover slowly.
- **Per-report timeout** (e.g. 120 s) with at most one retry for a timeout — today a slow
  card can hold a worker for 4 × 240 s. Slow cards are marked "slow" and retried later.
- **Tiering:** run Core reports for all centers before the extra cards.

## 5. Job queue decision (to settle)

| Option | Fits | Notes |
|---|---|---|
| Keep the file-based worker (today) | Works, tested | No retries/schedule/priority built in; we would write them |
| **Huey with SQLite** | Local, no extra server | Retries, delays, scheduling, priorities; sync code like ours |
| Taskiq | Async-first | Needs a broker: Redis, or ZeroMQ for local-only; our scraper is synchronous `requests` code |
| Celery / Dramatiq / RQ | Mature | Need Redis or RabbitMQ — not native on Windows |
| Prefect / Dagster | Pipeline UI | Heavy for a local single-user tool |

## 6. Test plan
1. Unit: store, merge, migrations, throttling, state machine (no network).
2. API: every endpoint, security guards, job lifecycle, reload survival (exists, extend).
3. **Real smoke tests** on a fixed set: centers 1 (control), 7 and 15 (empty perf), plus one
   INACTIVE center. Expected row counts recorded; response samples saved with personal
   data removed so contract tests can run offline.
4. Soak: 20 centers × all reports with 2 workers; watch back-off, slow cards, memory.
5. Batches toward all 541 ACTIVE centers (e.g. 50 at a time), each a resumable session.
6. Verify against Kronos for 3 centers by eye.

Acceptance: a full run finishes or can be continued; coverage explains every missing cell;
no manual file editing is needed.

## 7. Phases
| Phase | Content |
|---|---|
| P0 Stabilise | Per-report timeout, slow/empty states, known-empty registry, adaptive throttling |
| P1 Panel core | Home, Run (speed profiles, estimates, pause, retry), Live monitor matrix |
| P2 Data | Data browser, session compare, archive/export |
| P3 Quality | Automatic checks, spot-check helper, trends in Analytics |
| P4 Automation | Queue (per decision above), schedules, Settings, Logs/Health, Help |
