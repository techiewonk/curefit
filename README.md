# Curefit / Kronos Sales Scraper

Logs into the Curefit Kronos portal with a **manual OTP**, pulls Metabase report data
per center, stores everything in a local **SQLite** database, and exports versioned
**CSV** reports plus a standalone **HTML dashboard**.

Two reports are scraped per center (config-driven — easy to add more):

- **sales** — member-level sales list (User ID, Name, Phone, Pack, End Date, Active, Renewed, Revenue)
- **perf** — center KPI metrics (Total / New / Repeat / Online revenue, etc.)

Progress and logs are shown with a `rich` CLI dashboard (live progress bars, summary
panel, per-center table) and `structlog` structured logging.

---

## Quick start

```
1. install_requirements.bat        (once)
2. edit centers_to_process.csv     (add the center ids you want)
3. run.bat                         (type the OTP, then the dates — done)
```

---

## 1. Setup (once)

1. Install [Python 3](https://www.python.org/downloads/) (tick *Add Python to PATH* during install).
2. Double-click **`install_requirements.bat`** — it checks Python, upgrades pip, and installs
   dependencies from `requirements.txt` (`requests`, `rich`, `structlog`).

   Or manually:
   ```
   pip install -r requirements.txt
   ```

---

## 2. Pick which centers to scrape

Edit **`centers_to_process.csv`** — one center per row under the `center_id` column.
Only the centers listed here are scraped, so keep it short.

```
center_id,center_name
1,Cult Indiranagar
2,Cult JP Nagar
3,Cult HSR 19th Main
```

`centers.csv` is the full master list of ~1,400 centers (id, name, code, city, type,
cluster, etc.) — use it to look up the ids you need. Regenerate it any time with
`python curefit_login.py centers`.

---

## 3. Run everything (one command)

Double-click **`run.bat`**, or:

```
python run.py
```

This single auto flow will:

1. **Log in** — sends an OTP to the account and **asks you to type it at the terminal**.
2. **Ask the period** — prompts for **start date** and **end date** (defaults to last month).
3. **Scrape** every center in `centers_to_process.csv` for all reports.
4. **Export** the CSV reports + HTML dashboard and print a live progress + summary dashboard.

The account email is set at the top of `curefit_login.py` (`EMAIL = ...`).

> The OTP is single-use and the session expires after a while — `run.py` always logs in
> fresh at the start, so you just type the new code each run.

---

## Advanced / manual use

Run steps individually if you prefer:

```
python curefit_login.py            # interactive login only (asks for OTP)
python scrape_sales.py             # scrape only (uses an existing session + config dates)

python scrape_sales.py --start 2026-06-01 --end 2026-06-30   # different period
python scrape_sales.py --centers-csv my_list.csv             # different center list
python scrape_sales.py --only sales                          # just one report
python scrape_sales.py --refresh                             # re-scrape already-done centers
```

Login can also be driven step by step: `python curefit_login.py send` →
`validate <otp>` → `session`. Defaults (dates, DB name, delays, dashboard IDs) live in the
**CONFIG block** at the top of `scrape_sales.py`.

---

## 4. Outputs

Every run writes to the folder, timestamped. If a filename already exists it adds
`_v2`, `_v3`, … so nothing is overwritten.

| File | What |
|------|------|
| `curefit.db` | SQLite DB — tables `sales`, `perf_metrics`, `scrape_log` |
| `sales_report_<period>_<time>.csv` | Member-level sales rows |
| `perf_long_<period>_<time>.csv` | Perf metrics in long form (date / metric / value) |
| `perf_pivot_<period>_<time>.csv` | Perf metrics pivoted — one row per center, metrics as columns |
| `analysis_<period>_<time>.csv` | Per-center summary: sales count, sales revenue, renewals + renewal rate, total / new / repeat revenue and % |
| `dashboard_<period>_<time>.html` | Standalone dashboard — open by double-click, no server needed |

The `dashboard_*.html` file is a **snapshot** of one run (data baked in, opens by
double-click). For a **live** view that always reflects the current database, use the
dashboard server below.

---

## Live dashboard (auto-reads the DB)

One page that reads straight from `curefit.db` every time — no re-export needed. After any
scrape, just reload the page to see the new data.

Double-click **`dashboard.bat`**, or:

```
python dashboard_server.py
python dashboard_server.py --db curefit.db --port 8000   # options
```

It starts a small local server and opens `http://localhost:8000/` in your browser
(standard library only — no extra dependencies). The page lets you:

- pick any scraped **period** from a dropdown (and **Reload** for fresh data),
- see summary cards: centers, sales count, sales/total/new/repeat revenue, **avg ticket**, **active rate**,
- view **revenue-by-center** and **New-vs-Repeat** charts,
- read the full **per-center analysis** table (incl. active % and avg ticket),
- explore the **pack-mix** breakdown (revenue-share doughnut + table),
- **click any center row** to drill into its member-level sales.

Stop the server with `Ctrl+C`.

---

## How it works

For each center and report the scraper:

1. `POST https://kronos.curefit.co/v1/metabase/iframeUrl` (using the logged-in session)
   → returns a signed Metabase JWT for that center/dashboard.
2. `GET https://metabase.curefit.co/api/embed/dashboard/<jwt>/dashcard/<id>/card/<id>?parameters=…`
   → returns the raw rows, which are stored in SQLite.

**Politeness / reliability:** randomized 1–3s delay between requests, retry-with-backoff
(4 attempts) on errors, and a 5-minute request timeout so slow Metabase queries still return.
`scrape_log` tracks completed (report, center, period) combos so re-runs resume where they
left off instead of re-fetching everything (override with `--refresh`).

---

## Add another report

Drop a new entry into the `REPORTS` dict in `scrape_sales.py`:

```python
"my_report": {
    "dashboard": 1234, "dashcard": 5678, "card": 9012,
    "referer": "https://kronos.curefit.co/reports/my-report",
    "iframe_query": lambda cid, s, e: {"start": s, "end": e},
    "data_params": lambda s, e: {"start": s, "end": e},
},
```

Get the `dashboard` / `dashcard` / `card` ids and the exact query parameters from your
browser's DevTools → Network tab while viewing the report. Add a matching `store_*`
function if the columns differ from the existing reports.

---

## Troubleshooting

- **`python` is not recognized** — Python isn't on PATH. Reinstall Python 3 and tick
  *Add Python to PATH*, or use the full path to `python.exe`.
- **OTP not accepted** — codes are single-use and time-limited. `run.py` sends a fresh one
  each run; if it expired, just start `run.bat` again and type the new code.
- **Auth / 401 / empty results mid-run** — the session expired. Re-run `run.bat` (it logs
  in fresh at the start).
- **A center failed** — it's logged as `center_failed` and skipped; the run continues.
  Re-run to retry just the missing centers (finished ones are skipped automatically).
- **Slow perf queries** — Metabase can take a while the first time; the 5-minute request
  timeout and retry-with-backoff handle this. Later runs are faster (server-side cache).
- **No emojis / boxes look odd** — use Windows Terminal or a modern console for the best
  `rich` output.

---

## Files

| File | Purpose |
|------|---------|
| `run.py` / `run.bat` | **Start here** — one-command auto flow (login → dates → scrape → dashboard) |
| `curefit_login.py` | Manual-OTP login + session management |
| `scrape_sales.py` | Multi-report scraper, SQLite storage, CSV/HTML export |
| `dashboard_server.py` / `dashboard.bat` | Live dashboard that auto-reads `curefit.db` in the browser |
| `logutil.py` | Shared rich console + structured logging setup |
| `centers_to_process.csv` | **Edit this** — the centers you want to scrape |
| `centers.csv` / `centers.json` | Full master centers list (generated) |
| `requirements.txt` | Python dependencies |
| `install_requirements.bat` | One-click dependency installer (Windows) |
| `session.json` | Saved login session (auto-generated — keep private) |
| `curefit.db` | SQLite results database (auto-generated) |

> **Note:** `session.json` holds a live login token — keep it private and don't commit it.

---

## Sessions (one folder per run)

Every run is a **session** with its own folder, so a new run is never blocked by data that
an older run already saved:

```
runs/2026-10-20/2026-07-01_to_2026-07-31_a1b2c3/
  session.json                 period, centers, status (running / incomplete / complete)
  centers/1_Cult-Indiranagar/  one folder per center, one CSV per section
      sales.csv  perf.csv  gym.csv  perf_membership.csv ...
  sales_report.csv  perf_long.csv  perf_pivot.csv  gym_members.csv  analysis.csv
  dashboard.html  coverage.csv
```

- A new run always scrapes everything fresh.
- If the previous session for the same period did not finish, `run.bat` offers to
  **continue** it: only the center/section pairs that are still missing are fetched.
  From the command line: `python scrape_sales.py --start ... --end ... --session a1b2c3`.
- `curefit.db` stays the shared database; every row is tagged with its session id, and
  rows from before sessions are tagged `legacy`.
- `dashboard.bat` has a session picker, so each run can be viewed on its own.

### Merged files (all centers in one file per report)

At the end of every run, `merged/` inside the session folder gets one CSV per report
(`sales.csv`, `perf.csv`, `gym.csv`, `perf_membership.csv`, ...), each with every center's rows
and `center_id` / `center_name` as the first columns, plus `all_reports.xlsx` with one sheet per
report. To rebuild them for an earlier session: `python scrape_sales.py --merge a1b2c3`.
