"""
Curefit / Kronos multi-report scraper -> local SQLite + versioned CSV reports.

Three reports are scraped per center (config-driven, add more in REPORTS):
  - sales : "Expired Pack User Details - Upto Last 90 Days" (dashboard 2536 / dashcard 19822 / card 39684)
  - perf  : "Total Revenue and Split", monthly KPIs         (dashboard 1750 / dashcard 13395 / card 24755)
  - gym   : "My Gym Member", member/pack list               (dashboard 1750 / dashcard 13454 / card 24811)

Chain per (center, report):
  1. POST kronos /v1/metabase/iframeUrl (logged-in session)  -> signed metabase JWT
  2. GET  metabase /api/embed/.../dashcard/<dashcard>/card/<card>?parameters=...  -> rows

Politeness: randomized delay between requests + retry-with-backoff on errors.

Prereq: run curefit_login.py (send/validate/session) so session.json holds a live session.
Which centers get scraped is driven by centers_to_process.csv (column: center_id).

Usage:
  python scrape_sales.py
  python scrape_sales.py --centers-csv my_list.csv --start 2026-06-01 --end 2026-06-30
  python scrape_sales.py --only sales
  python scrape_sales.py --refresh
"""
import argparse
import csv
import json
import os
import random
import re
import sqlite3
import threading
import time
import uuid
import urllib.parse

import requests
from concurrent.futures import ThreadPoolExecutor
from rich.panel import Panel
from rich.progress import (Progress, SpinnerColumn, BarColumn, TextColumn,
                           MofNCompleteColumn, TimeElapsedColumn)
from rich.table import Table

import curefit_login as C
from logutil import console, get_logger

log = get_logger()

# ======================= CONFIG (edit these) =======================
CENTERS_CSV = "centers_to_process.csv"
START_DATE = "2026-07-01"
END_DATE = "2026-07-31"
DB_FILE = "curefit.db"

# request politeness
DELAY_MIN = 1.0          # min seconds between requests
DELAY_MAX = 3.0          # max seconds between requests
MAX_RETRIES = 4          # retries per failed request
BACKOFF_BASE = 2.0       # exponential backoff base (seconds)
ROW_CAP = 10000          # Metabase's default result cap; a 3,293-row result was seen intact, so 2,000 is not the limit
POLL_SECONDS = 240        # how long to wait for a slow Metabase query (HTTP 202 / not completed)
AUTH_FAIL_LIMIT = 3      # consecutive 401/403s before the run stops and asks for a re-login
REPORT_FAIL_LIMIT = 3    # a report failing this many times in a row with no success is skipped
TIMEOUT = 300            # per-request read timeout (seconds) - metabase can be slow

# report definitions -- add a new dict here to scrape another dashboard
REPORTS = {
    "sales": {
        "dashboard": 2536, "dashcard": 19822, "card": 39684,
        "referer": "https://kronos.curefit.co/reports/sales_perfromance",
        # kronos iframeUrl query and metabase data-call parameters differ per report
        "iframe_query": lambda cid, s, e: {"start_date": s, "end_date": e, "center_id": str(cid)},
        "data_params": lambda s, e: {"start_date": s, "end_date": e},
    },
    "perf": {
        "dashboard": 1750, "dashcard": 13395, "card": 24755,
        "referer": "https://kronos.curefit.co/reports/perf-dashboard",
        "iframe_query": lambda cid, s, e: {"start": s, "end": e},
        "data_params": lambda s, e: {"start": s, "end": e, "granular": "month"},
    },
    "gym": {
        "dashboard": 1750, "dashcard": 13454, "card": 24811,
        "referer": "https://kronos.curefit.co/reports/perf-dashboard",
        "iframe_query": lambda cid, s, e: {"start": s, "end": e},
        "data_params": lambda s, e: {"start": s, "end": e, "granular": "month"},
    },
}


def _perf_card(dashcard, card):
    return {"dashboard": 1750, "dashcard": dashcard, "card": card,
            "referer": "https://kronos.curefit.co/reports/perf-dashboard",
            "iframe_query": lambda cid, s, e: {"start": s, "end": e},
            "data_params": lambda s, e: {"start": s, "end": e, "granular": "month"}}


def _sales_card(dashcard, card):
    return {"dashboard": 2536, "dashcard": dashcard, "card": card,
            "referer": "https://kronos.curefit.co/reports/sales_perfromance",
            "iframe_query": lambda cid, s, e: {"start_date": s, "end_date": e, "center_id": str(cid)},
            "data_params": lambda s, e: {"start_date": s, "end_date": e}}


# extra cards, stored generically (card columns are not known in advance)
GENERIC_REPORTS = {
    "perf_pack_purchase": _perf_card(13385, 24754),      # Pack Purchase and Split
    "perf_packs_split": _perf_card(13423, 24773),        # Packs Split
    "perf_membership": _perf_card(13424, 24774),         # Membership Count
    "perf_expiries": _perf_card(14436, 25931),           # Expiries and Retention
    "perf_trial_funnel": _perf_card(13426, 24776),       # Trial Funnel
    "perf_walkin_funnel": _perf_card(13432, 24783),      # Walkin Funnel
    "perf_workout_feedback": _perf_card(13429, 24779),   # Workout Feedback
    "perf_ratings": _perf_card(13431, 24781),            # Ratings
    "perf_trainer_rating": _perf_card(18537, 34056),     # Trainer level rating
    "sales_center_trials": _sales_card(19818, 39680),    # Center Overview (Trials)
    "sales_cm_trials": _sales_card(19819, 39681),        # CM Overview (Trials)
    "sales_lead_view": _sales_card(19817, 39679),        # Lead View (Assigned Manager) - 30 days
    "sales_lead_quality": _sales_card(21562, 43869),     # Lead Quality & Conversion
    "sales_center_walkins": _sales_card(19820, 39682),   # Center Overview (Walk-Ins)
    "sales_cm_walkins": _sales_card(19821, 39683),       # CM Overview (Walk-Ins)
}
REPORTS.update(GENERIC_REPORTS)
# ===================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
CENTERS_JSON = os.path.join(HERE, "centers.json")
RUNS_DIR = os.environ.get("CUREFIT_RUNS_DIR") or os.path.join(HERE, "runs")


def polite_sleep():
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


def load_center_meta():
    meta = {}
    if os.path.exists(CENTERS_JSON):
        with open(CENTERS_JSON, encoding="utf-8") as f:
            data = json.load(f)
        for c in data.get("centers", []):
            meta[str(c["id"])] = (c.get("name", "").strip(), c.get("centerCode", ""))
    return meta


def read_center_ids(path):
    ids = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames and "center_id" in reader.fieldnames:
            for row in reader:
                v = (row.get("center_id") or "").strip()
                if v and not v.startswith("#"):
                    ids.append(v)
        else:
            f.seek(0)
            for line in f:
                v = line.strip()
                if v and not v.startswith("#") and v.lower() != "center_id":
                    ids.append(v)
    return ids


# --------------------------- sessions ---------------------------
# Every run lives in runs/<date>/<start>_to_<end>_<id>/ with a session.json, one folder per
# center holding one CSV per section, and the period-level reports.
def _slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-") or "center"


def _save_session(info):
    with open(os.path.join(info["folder"], "session.json"), "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in info.items() if k != "folder"}, f, indent=2)


def new_session(start, end, ids, reports):
    sid = uuid.uuid4().hex[:6]
    folder = os.path.join(RUNS_DIR, time.strftime("%Y-%m-%d"), f"{start}_to_{end}_{sid}")
    os.makedirs(folder, exist_ok=True)
    info = {"session_id": sid, "start": start, "end": end, "status": "running",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "finished_at": None,
            "centers": ids, "reports": reports, "folder": folder}
    _save_session(info)
    return info


def list_sessions():
    """All sessions on disk, newest first."""
    out = []
    if os.path.isdir(RUNS_DIR):
        for day in os.listdir(RUNS_DIR):
            day_dir = os.path.join(RUNS_DIR, day)
            if not os.path.isdir(day_dir):
                continue
            for name in os.listdir(day_dir):
                f = os.path.join(day_dir, name, "session.json")
                if os.path.exists(f):
                    with open(f, encoding="utf-8") as fh:
                        info = json.load(fh)
                    info["folder"] = os.path.join(day_dir, name)
                    out.append(info)
    return sorted(out, key=lambda i: i.get("created_at", ""), reverse=True)


def find_session(sid):
    return next((i for i in list_sessions() if i["session_id"] == sid), None)


def find_unfinished(start, end):
    """Latest session for this exact period that did not complete."""
    return next((i for i in list_sessions()
                 if i["start"] == start and i["end"] == end and i["status"] != "complete"), None)


TYPED_COLUMNS = {
    "sales": ["user_id", "name", "phone", "pack_name", "member_end_date",
              "active_last_30d", "renewed", "revenue"],
    "perf": ["metric_date", "metric", "value"],
    "gym": ["userid", "username", "packname", "final_amountpaid", "Purchasedate",
            "Packstartdate", "Packenddate", "status", "offline_vs_online", "new_vs_repeat",
            "Userclasslevel", "last_class_date"],
}


def write_center_file(folder, cid, name, code, rep_name, cols, rows):
    """centers/<id>_<name>/<section>.csv inside the session folder."""
    width = len(rows[0]) if rows else 0
    header = cols if cols and len(cols) == width else \
        (TYPED_COLUMNS.get(rep_name) if len(TYPED_COLUMNS.get(rep_name, [])) == width else None) \
        or [f"col{i + 1}" for i in range(width)]
    d = os.path.join(folder, "centers", f"{cid}_{_slug(name or code)}")
    os.makedirs(d, exist_ok=True)
    _write_csv(os.path.join(d, f"{rep_name}.csv"), header, rows)


# ---------------------------- DB ----------------------------
def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sales (
        center_id TEXT, center_name TEXT, center_code TEXT,
        start_date TEXT, end_date TEXT,
        user_id TEXT, name TEXT, phone TEXT, pack_name TEXT, member_end_date TEXT,
        active_last_30d TEXT, renewed TEXT, revenue REAL, scraped_at TEXT, session_id TEXT
    );
    CREATE TABLE IF NOT EXISTS perf_metrics (
        center_id TEXT, center_name TEXT, center_code TEXT,
        start_date TEXT, end_date TEXT,
        metric_date TEXT, metric TEXT, value REAL, scraped_at TEXT, session_id TEXT
    );
    CREATE TABLE IF NOT EXISTS gym_members (
        center_id TEXT, center_name TEXT, center_code TEXT,
        start_date TEXT, end_date TEXT,
        user_id TEXT, username TEXT, pack_name TEXT, final_amount_paid REAL,
        purchase_date TEXT, pack_start_date TEXT, pack_end_date TEXT, status TEXT,
        offline_vs_online TEXT, new_vs_repeat TEXT, class_level TEXT, last_class_date TEXT,
        scraped_at TEXT, session_id TEXT
    );
    CREATE TABLE IF NOT EXISTS card_rows (
        report TEXT, center_id TEXT, center_name TEXT, center_code TEXT,
        start_date TEXT, end_date TEXT, row_num INTEGER, data TEXT, scraped_at TEXT,
        session_id TEXT
    );
    CREATE TABLE IF NOT EXISTS card_columns (report TEXT PRIMARY KEY, columns TEXT);
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY, start_date TEXT, end_date TEXT, status TEXT,
        created_at TEXT, finished_at TEXT, folder TEXT,
        centers TEXT, reports TEXT, coverage TEXT, missing TEXT
    );
    CREATE TABLE IF NOT EXISTS scrape_log (
        report TEXT, center_id TEXT, start_date TEXT, end_date TEXT,
        row_count INTEGER, scraped_at TEXT, session_id TEXT,
        PRIMARY KEY (session_id, report, center_id, start_date, end_date)
    );
    """)
    _migrate_sales(conn)
    _migrate_sessions(conn)
    conn.executescript("""
    CREATE INDEX IF NOT EXISTS idx_sales_sess ON sales (session_id, center_id, start_date, end_date);
    CREATE INDEX IF NOT EXISTS idx_perf_sess ON perf_metrics (session_id, center_id, start_date, end_date);
    CREATE INDEX IF NOT EXISTS idx_gym_sess ON gym_members (session_id, center_id, start_date, end_date);
    CREATE INDEX IF NOT EXISTS idx_card_sess ON card_rows
        (session_id, report, center_id, start_date, end_date);
    """)
    conn.commit()


def save_session_row(conn, info, coverage=None):
    """Mirror session.json into the database so an admin view can list and check sessions."""
    conn.execute(
        "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (info["session_id"], info["start"], info["end"], info["status"], info["created_at"],
         info.get("finished_at"), info["folder"], json.dumps(info.get("centers", [])),
         json.dumps(info.get("reports", [])), json.dumps(coverage) if coverage else None,
         json.dumps(info.get("missing", {}))))
    conn.commit()


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _migrate_sessions(conn):
    """Databases from before sessions: tag every existing row as session 'legacy'."""
    for t in ("sales", "gym_members", "card_rows"):
        if "session_id" not in _columns(conn, t):
            conn.execute(f"ALTER TABLE {t} ADD COLUMN session_id TEXT")
            conn.execute(f"UPDATE {t} SET session_id='legacy'")
    if "session_id" not in _columns(conn, "perf_metrics"):  # old primary key forbids sessions
        conn.executescript("""
        ALTER TABLE perf_metrics RENAME TO perf_old;
        CREATE TABLE perf_metrics (
            center_id TEXT, center_name TEXT, center_code TEXT,
            start_date TEXT, end_date TEXT,
            metric_date TEXT, metric TEXT, value REAL, scraped_at TEXT, session_id TEXT
        );
        INSERT INTO perf_metrics SELECT *, 'legacy' FROM perf_old;
        DROP TABLE perf_old;
        """)
    if "session_id" not in _columns(conn, "scrape_log"):
        conn.executescript("""
        ALTER TABLE scrape_log RENAME TO log_old;
        CREATE TABLE scrape_log (
            report TEXT, center_id TEXT, start_date TEXT, end_date TEXT,
            row_count INTEGER, scraped_at TEXT, session_id TEXT,
            PRIMARY KEY (session_id, report, center_id, start_date, end_date)
        );
        INSERT INTO scrape_log SELECT *, 'legacy' FROM log_old;
        DROP TABLE log_old;
        """)
    conn.commit()


def _migrate_sales(conn):
    """Old schema keyed sales on (center, period, user, pack), which merged repeat purchases."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sales'").fetchone()[0]
    if "PRIMARY KEY" not in sql:
        return
    conn.executescript("""
    ALTER TABLE sales RENAME TO sales_old;
    CREATE TABLE sales (
        center_id TEXT, center_name TEXT, center_code TEXT,
        start_date TEXT, end_date TEXT,
        user_id TEXT, name TEXT, phone TEXT, pack_name TEXT, member_end_date TEXT,
        active_last_30d TEXT, renewed TEXT, revenue REAL, scraped_at TEXT
    );
    INSERT INTO sales SELECT * FROM sales_old;
    DROP TABLE sales_old;
    CREATE INDEX IF NOT EXISTS idx_sales_period ON sales (center_id, start_date, end_date);
    """)


# ------------------------- HTTP core -------------------------
class AuthError(Exception):
    """Session rejected (401/403): retrying is pointless, a re-login is needed."""


def _with_retry(fn, what):
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            if code in (401, 403):
                raise AuthError(f"{what}: HTTP {code}") from e
            if code in (400, 404):
                raise
            last = e
        except Exception as e:
            last = e
        wait = BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 1)
        log.warning("retry", what=what, error=str(last), attempt=attempt + 1,
                    of=MAX_RETRIES, wait_s=round(wait, 1))
        time.sleep(wait)
    raise last


def get_token(session, rep, center_id, start, end):
    def do():
        payload = {
            "payload": {"resource": {"dashboard": rep["dashboard"]},
                        "params": {"center_id": str(center_id)}},
            "query": rep["iframe_query"](center_id, start, end),
            "fragmentIdentifier": "bordered=true&titled=true",
        }
        r = session.post("https://kronos.curefit.co/v1/metabase/iframeUrl", json=payload,
                         headers={"referer": rep["referer"]}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()["iframeUrl"].split("/dashboard/")[1].split("?")[0]
    return _with_retry(do, "iframeUrl")


def _embed_headers(token):
    return {
        "accept": "application/json",
        "x-metabase-client": "embedding-iframe",
        "x-metabase-embedded": "true",
        "referer": f"https://metabase.curefit.co/embed/dashboard/{token}",
        "user-agent": C.UA,
    }


def warm_up(mb, token):
    """Mimic the browser's dashboard load (session properties + dashboard definition).

    Best effort: a failure here is logged but never blocks the data call.
    """
    hdr = _embed_headers(token)
    for url in ("https://metabase.curefit.co/api/session/properties",
                f"https://metabase.curefit.co/api/embed/dashboard/{token}"
                f"?dashboard_load_id={uuid.uuid4()}"):
        try:
            mb.get(url, headers=hdr, timeout=TIMEOUT).raise_for_status()
        except Exception as e:
            log.warning("warmup_failed", url=url.split("?")[0], error=str(e))


def get_rows(mb, rep, token, start, end):
    """Return (rows, column_names) once Metabase reports the query completed."""
    params = urllib.parse.quote(json.dumps(rep["data_params"](start, end)))
    url = (f"https://metabase.curefit.co/api/embed/dashboard/{token}"
           f"/dashcard/{rep['dashcard']}/card/{rep['card']}?parameters={params}")
    hdr = {**_embed_headers(token), "x-metabase-guest-embed": "true"}

    def do():
        deadline = time.time() + POLL_SECONDS
        status = None
        while True:
            r = mb.get(url, headers=hdr, timeout=TIMEOUT)
            r.raise_for_status()
            try:  # a 200 *or* 202 body can already be the finished result
                j = r.json()
            except ValueError:
                j = {}
            status = j.get("status") if isinstance(j, dict) else None
            if status == "completed":
                data = j["data"]
                return data["rows"], [c.get("name") for c in data.get("cols", [])]
            if status == "failed":
                raise RuntimeError(f"metabase query failed: {j.get('error')}")
            if time.time() > deadline:
                raise RuntimeError(f"metabase query not completed after {POLL_SECONDS}s "
                                   f"(status={status!r})")
            time.sleep(2)
    return _with_retry(do, "data")


# ------------------------- persistence -------------------------
def store_sales(conn, cid, name, code, start, end, rows, now, sid):
    conn.execute("DELETE FROM sales WHERE session_id=? AND center_id=? AND start_date=? AND end_date=?",
                 (sid, cid, start, end))
    for r in rows:  # [user_id,name,phone,pack,end,active,renewed,revenue]
        conn.execute("INSERT INTO sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, name, code, start, end, str(r[0]), r[1], r[2], r[3], r[4],
                      r[5], r[6], r[7], now, sid))


def store_perf(conn, cid, name, code, start, end, rows, now, sid):
    conn.execute("DELETE FROM perf_metrics WHERE session_id=? AND center_id=? "
                 "AND start_date=? AND end_date=?", (sid, cid, start, end))
    for r in rows:  # [date, metric, value]
        conn.execute("INSERT INTO perf_metrics VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (cid, name, code, start, end, r[0], (r[1] or "").strip(), r[2], now, sid))


def store_gym(conn, cid, name, code, start, end, rows, now, sid):
    conn.execute("DELETE FROM gym_members WHERE session_id=? AND center_id=? "
                 "AND start_date=? AND end_date=?", (sid, cid, start, end))
    for r in rows:  # [userid, username, pack, amount, purchased, pack_start, pack_end, status,
        #             offline_vs_online, new_vs_repeat, class_level, last_class_date]
        conn.execute("INSERT INTO gym_members VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, name, code, start, end, str(r[0]), *r[1:12], now, sid))


STORE = {"sales": store_sales, "perf": store_perf, "gym": store_gym}


def store_generic(conn, report, cols, cid, name, code, start, end, rows, now, sid):
    """Cards whose columns are not modelled: one JSON row each, columns kept per report."""
    conn.execute("DELETE FROM card_rows WHERE session_id=? AND report=? AND center_id=? "
                 "AND start_date=? AND end_date=?", (sid, report, cid, start, end))
    conn.executemany(
        "INSERT INTO card_rows VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(report, cid, name, code, start, end, i, json.dumps(r, default=str), now, sid)
         for i, r in enumerate(rows)])
    conn.execute("INSERT OR REPLACE INTO card_columns VALUES (?,?)", (report, json.dumps(cols)))


# --------------------------- reports ---------------------------
def versioned_path(base_no_ext, ext=".csv"):
    p = base_no_ext + ext
    if not os.path.exists(p):
        return p
    n = 2
    while os.path.exists(f"{base_no_ext}_v{n}{ext}"):
        n += 1
    return f"{base_no_ext}_v{n}{ext}"


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def export_reports(conn, start, end, sid, out_dir):
    """Period-level reports for one session, written into the session folder."""
    written = []

    def out(name):
        return os.path.join(out_dir, name)

    # 1) sales member-level report
    cols = ["center_id", "center_name", "center_code", "start_date", "end_date",
            "user_id", "name", "phone", "pack_name", "member_end_date",
            "active_last_30d", "renewed", "revenue", "scraped_at"]
    rows = conn.execute(
        f"SELECT {','.join(cols)} FROM sales WHERE session_id=? AND start_date=? AND end_date=? "
        f"ORDER BY CAST(center_id AS INT), name", (sid, start, end)).fetchall()
    _write_csv(out("sales_report.csv"), cols, rows); written.append(out("sales_report.csv"))

    # 2) perf long report
    pcols = ["center_id", "center_name", "center_code", "start_date", "end_date",
             "metric_date", "metric", "value", "scraped_at"]
    prows = conn.execute(
        f"SELECT {','.join(pcols)} FROM perf_metrics WHERE session_id=? AND start_date=? "
        f"AND end_date=? ORDER BY CAST(center_id AS INT), metric", (sid, start, end)).fetchall()
    _write_csv(out("perf_long.csv"), pcols, prows); written.append(out("perf_long.csv"))

    # 3) perf pivoted wide (one row per center, metrics as columns)
    metrics = [m[0] for m in conn.execute(
        "SELECT DISTINCT metric FROM perf_metrics WHERE session_id=? AND start_date=? "
        "AND end_date=? ORDER BY metric", (sid, start, end))]
    centers = conn.execute(
        "SELECT DISTINCT center_id, center_name FROM perf_metrics "
        "WHERE session_id=? AND start_date=? AND end_date=? ORDER BY CAST(center_id AS INT)",
        (sid, start, end))
    pivot_rows = []
    for cid, cname in centers.fetchall():
        vals = dict(conn.execute(
            "SELECT metric, value FROM perf_metrics WHERE session_id=? AND center_id=? "
            "AND start_date=? AND end_date=?", (sid, cid, start, end)).fetchall())
        pivot_rows.append([cid, cname] + [vals.get(m) for m in metrics])
    _write_csv(out("perf_pivot.csv"), ["center_id", "center_name"] + metrics, pivot_rows)
    written.append(out("perf_pivot.csv"))

    # 3b) gym members (member/pack list)
    gcols = ["center_id", "center_name", "center_code", "start_date", "end_date",
             "user_id", "username", "pack_name", "final_amount_paid", "purchase_date",
             "pack_start_date", "pack_end_date", "status", "offline_vs_online",
             "new_vs_repeat", "class_level", "last_class_date", "scraped_at"]
    grows = conn.execute(
        f"SELECT {','.join(gcols)} FROM gym_members WHERE session_id=? AND start_date=? "
        f"AND end_date=? ORDER BY CAST(center_id AS INT), username", (sid, start, end)).fetchall()
    _write_csv(out("gym_members.csv"), gcols, grows); written.append(out("gym_members.csv"))

    # 3c) generic cards: one CSV per report that has data in this session
    for rname in GENERIC_REPORTS:
        row = conn.execute("SELECT columns FROM card_columns WHERE report=?", (rname,)).fetchone()
        data = conn.execute(
            "SELECT center_id, center_name, center_code, data, scraped_at FROM card_rows "
            "WHERE session_id=? AND report=? AND start_date=? AND end_date=? "
            "ORDER BY CAST(center_id AS INT), row_num", (sid, rname, start, end)).fetchall()
        if not row or not data:
            continue
        cols = json.loads(row[0])
        rows_out = []
        for cid, cn, cc, d, scraped in data:
            vals = json.loads(d)
            vals += [None] * (len(cols) - len(vals))
            rows_out.append([cid, cn, cc] + vals + [scraped])
        _write_csv(out(f"{rname}.csv"),
                   ["center_id", "center_name", "center_code"] + cols + ["scraped_at"], rows_out)
        written.append(out(f"{rname}.csv"))

    # 4) analysis: revenue summary + new/repeat split + combined
    analysis = build_analysis(conn, start, end, metrics, sid)
    _write_csv(out("analysis.csv"), analysis["header"], analysis["rows"])
    written.append(out("analysis.csv"))

    # 5) standalone HTML dashboard (data embedded, no server needed)
    _write_html(out("dashboard.html"), start, end, analysis)
    written.append(out("dashboard.html"))

    return written, analysis


def _write_html(path, start, end, analysis):
    header, rows = analysis["header"], analysis["rows"]
    data_json = json.dumps({"header": header, "rows": rows})
    totals = {
        "sales_revenue": sum((r[3] or 0) for r in rows),
        "total_revenue": sum((r[6] or 0) for r in rows),
        "new_revenue": sum((r[7] or 0) for r in rows),
        "repeat_revenue": sum((r[8] or 0) for r in rows),
        "sales_count": sum((r[2] or 0) for r in rows),
    }
    html = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Curefit Sales Dashboard __START__ to __END__</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
 body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
 header{padding:20px 28px;background:#161a22;border-bottom:1px solid #262b36}
 h1{margin:0;font-size:20px} .sub{color:#8b93a1;font-size:13px;margin-top:4px}
 .wrap{padding:24px 28px;max-width:1200px;margin:0 auto}
 .cards{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px}
 .card{background:#161a22;border:1px solid #262b36;border-radius:10px;padding:16px 20px;min-width:170px}
 .card .lbl{color:#8b93a1;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
 .card .val{font-size:24px;font-weight:600;margin-top:6px}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:24px}
 .panel{background:#161a22;border:1px solid #262b36;border-radius:10px;padding:18px}
 .panel h2{font-size:14px;margin:0 0 12px;color:#c7cdd8}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:8px 10px;text-align:right;border-bottom:1px solid #262b36}
 th:first-child,td:first-child{text-align:left}
 th{color:#8b93a1;font-weight:600;position:sticky;top:0;background:#161a22}
 tr:hover td{background:#1c212b}
 @media(max-width:900px){.grid{grid-template-columns:1fr}}
</style></head><body>
<header><h1>Curefit Sales Dashboard</h1><div class="sub">Period __START__ &rarr; __END__</div></header>
<div class="wrap">
 <div class="cards" id="cards"></div>
 <div class="grid">
   <div class="panel"><h2>Revenue by center</h2><canvas id="revChart" height="220"></canvas></div>
   <div class="panel"><h2>New vs Repeat revenue</h2><canvas id="mixChart" height="220"></canvas></div>
 </div>
 <div class="panel"><h2>Per-center detail</h2><div style="overflow:auto;max-height:520px"><table id="tbl"></table></div></div>
</div>
<script>
const DATA = __DATA__;
const TOTALS = __TOTALS__;
const fmt = n => n==null?'-':'₹'+Number(n).toLocaleString('en-IN');
const num = n => n==null?'-':Number(n).toLocaleString('en-IN');
// summary cards
const cards=[['Sales count',num(TOTALS.sales_count)],['Sales revenue',fmt(TOTALS.sales_revenue)],
 ['Total revenue',fmt(TOTALS.total_revenue)],['New revenue',fmt(TOTALS.new_revenue)],['Repeat revenue',fmt(TOTALS.repeat_revenue)]];
document.getElementById('cards').innerHTML=cards.map(c=>`<div class="card"><div class="lbl">${c[0]}</div><div class="val">${c[1]}</div></div>`).join('');
// table
const H=DATA.header, R=DATA.rows;
let t='<tr>'+H.map(h=>`<th>${h}</th>`).join('')+'</tr>';
t+=R.map(r=>'<tr>'+r.map((v,i)=>`<td>${v==null?'':( [3,6,7,8].includes(i)?fmt(v):v)}</td>`).join('')+'</tr>').join('');
document.getElementById('tbl').innerHTML=t;
// charts
const names=R.map(r=>r[1]||r[0]);
const idx=n=>H.indexOf(n);
new Chart(document.getElementById('revChart'),{type:'bar',data:{labels:names,
 datasets:[{label:'Total revenue',data:R.map(r=>r[idx('total_revenue')]),backgroundColor:'#4f8cff'}]},
 options:{plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#8b93a1'}},y:{ticks:{color:'#8b93a1'}}}}});
new Chart(document.getElementById('mixChart'),{type:'bar',data:{labels:names,
 datasets:[{label:'New',data:R.map(r=>r[idx('new_revenue')]),backgroundColor:'#33c48d'},
           {label:'Repeat',data:R.map(r=>r[idx('repeat_revenue')]),backgroundColor:'#f5a623'}]},
 options:{scales:{x:{stacked:true,ticks:{color:'#8b93a1'}},y:{stacked:true,ticks:{color:'#8b93a1'}}},
  plugins:{legend:{labels:{color:'#c7cdd8'}}}}});
</script></body></html>"""
    html = (html.replace("__START__", start).replace("__END__", end)
            .replace("__DATA__", data_json).replace("__TOTALS__", json.dumps(totals)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def _find_metric(metrics, *needles):
    for m in metrics:
        low = m.lower()
        if all(n in low for n in needles):
            return m
    return None


def build_analysis(conn, start, end, metrics, sid):
    m_total = _find_metric(metrics, "total", "revenue") or _find_metric(metrics, "1.", "total")
    m_new = _find_metric(metrics, "new", "revenue")
    m_repeat = _find_metric(metrics, "repeat", "revenue")

    header = ["center_id", "center_name", "sales_count", "sales_revenue",
              "renewals", "renewal_rate_%", "total_revenue", "new_revenue",
              "repeat_revenue", "new_%", "repeat_%"]
    rows = []
    # pull the center list from whatever we scraped (sales or perf)
    centers = conn.execute("""
        SELECT center_id, MAX(center_name) FROM (
            SELECT center_id, center_name FROM sales
              WHERE session_id=? AND start_date=? AND end_date=?
            UNION SELECT center_id, center_name FROM perf_metrics
              WHERE session_id=? AND start_date=? AND end_date=?
        ) GROUP BY center_id ORDER BY CAST(center_id AS INT)
    """, (sid, start, end, sid, start, end)).fetchall()

    for cid, cname in centers:
        sc = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(revenue),0), "
            "SUM(CASE WHEN renewed='Yes' THEN 1 ELSE 0 END) "
            "FROM sales WHERE session_id=? AND center_id=? AND start_date=? AND end_date=?",
            (sid, cid, start, end)).fetchone()
        count, sales_rev, renewals = sc[0], sc[1], sc[2] or 0
        rr = round(100.0 * renewals / count, 1) if count else 0.0

        def pv(metric):
            if not metric:
                return None
            row = conn.execute(
                "SELECT SUM(value) FROM perf_metrics WHERE session_id=? AND center_id=? "
                "AND metric=? AND start_date=? AND end_date=?",
                (sid, cid, metric, start, end)).fetchone()
            return row[0]
        total, new, repeat = pv(m_total), pv(m_new), pv(m_repeat)
        new_pct = round(100.0 * new / total, 1) if total and new is not None else None
        rep_pct = round(100.0 * repeat / total, 1) if total and repeat is not None else None
        rows.append([cid, cname, count, round(sales_rev, 0), renewals, rr,
                     round(total, 0) if total else None,
                     round(new, 0) if new is not None else None,
                     round(repeat, 0) if repeat is not None else None,
                     new_pct, rep_pct])
    return {"header": header, "rows": rows}


def _inr(v):
    return "-" if v is None else f"₹{v:,.0f}"


def print_analysis(analysis, start, end):
    rows = analysis["rows"]
    # summary cards
    tot = sum((r[6] or 0) for r in rows)
    new = sum((r[7] or 0) for r in rows)
    rep = sum((r[8] or 0) for r in rows)
    cnt = sum((r[2] or 0) for r in rows)
    cards = (f"[bold]{cnt:,}[/bold] sales    "
             f"total [bold green]{_inr(tot)}[/bold green]    "
             f"new [green]{_inr(new)}[/green]    repeat [yellow]{_inr(rep)}[/yellow]")
    console.print(Panel(cards, title=f"[bold]Revenue summary  {start} → {end}[/bold]",
                        border_style="cyan"))

    t = Table(title="Per-center analysis", header_style="bold cyan", expand=True)
    t.add_column("ID", justify="right")
    t.add_column("Center")
    t.add_column("Sales", justify="right")
    t.add_column("Sales rev", justify="right")
    t.add_column("Renew%", justify="right")
    t.add_column("Total rev", justify="right")
    t.add_column("New rev", justify="right")
    t.add_column("Repeat rev", justify="right")
    t.add_column("New%", justify="right")
    for r in rows:
        t.add_row(str(r[0]), str(r[1]), str(r[2]), _inr(r[3]),
                  "-" if r[5] is None else f"{r[5]}%",
                  _inr(r[6]), _inr(r[7]), _inr(r[8]),
                  "-" if r[9] is None else f"{r[9]}%")
    console.print(t)


# ----------------------------- merge -----------------------------
def merge_session(folder, meta=None):
    """One file per report with every center's rows, built from centers/*/<report>.csv.

    Writes merged/<report>.csv (center_id, center_name + the card's own columns) and, when
    openpyxl is available, merged/all_reports.xlsx with one sheet per report.
    """
    meta = meta if meta is not None else load_center_meta()
    centers_dir = os.path.join(folder, "centers")
    out_dir = os.path.join(folder, "merged")
    if not os.path.isdir(centers_dir):
        return []
    os.makedirs(out_dir, exist_ok=True)

    parts = {}  # report -> [(center_id, file)]
    for d in sorted(os.listdir(centers_dir),
                    key=lambda x: int(x.split("_")[0]) if x.split("_")[0].isdigit() else 0):
        cid = d.split("_")[0]
        for f in sorted(os.listdir(os.path.join(centers_dir, d))):
            if f.endswith(".csv"):
                parts.setdefault(f[:-4], []).append((cid, os.path.join(centers_dir, d, f)))

    written, merged = [], {}
    for report in [r for r in REPORTS if r in parts]:
        header, rows = [], []
        for cid, path in parts[report]:
            with open(path, newline="", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                cols = next(reader, [])
                for c in cols:  # centers can differ in columns: keep the union, in order seen
                    if c not in header:
                        header.append(c)
                name = meta.get(cid, ("", ""))[0]
                for r in reader:
                    rows.append((cid, name, dict(zip(cols, r))))
        full = ["center_id", "center_name"] + header
        data = [[cid, name] + [row.get(c, "") for c in header] for cid, name, row in rows]
        path = os.path.join(out_dir, f"{report}.csv")
        _write_csv(path, full, data)
        written.append(path)
        merged[report] = (full, data)

    try:
        from openpyxl import Workbook
        wb = Workbook(write_only=True)
        for report, (full, data) in merged.items():
            ws = wb.create_sheet(report[:31])
            ws.append(full)
            for r in data:
                ws.append(r)
        xlsx = os.path.join(out_dir, "all_reports.xlsx")
        wb.save(xlsx)
        written.append(xlsx)
    except ImportError:
        log.warning("xlsx_skipped", reason="openpyxl not installed (pip install openpyxl)")
    return written


# ----------------------------- runner -----------------------------
def _write_coverage(stats, path):
    rows = []
    for rep_name, st in stats.items():
        rows.append([rep_name, st["requested"], st["already_done"], st["ok"], len(st["empty"]),
                     len(st["failed"]), len(st["capped"]), "yes" if st["skipped"] else "",
                     " ".join(st["empty"]), " ".join(st["failed"])])
    _write_csv(path, ["report", "requested", "already_done", "scraped", "empty", "failed",
                      "possibly_truncated", "skipped_after_failures", "empty_center_ids",
                      "failed_center_ids"], rows)
    return rows


def run_scrape(start, end, centers_csv=CENTERS_CSV, db=DB_FILE, only=None, refresh=False,
               session=None, status=None, workers=1):
    """Scrape all configured reports for the centers in the CSV into one session.

    session=None starts a brand-new session (nothing is skipped because of older runs).
    Pass an existing session dict (see find_unfinished / find_session) to resume it: only
    the center/section pairs it has not saved yet are fetched.

    only: one report name or a list of them (default: all).
    status: optional dict the admin app reads for live progress; set status["stop"]=True to
    stop cleanly after the current request (the session stays resumable).
    workers: how many centers are fetched at the same time (1 = strictly one after another).
    """
    csv_path = centers_csv if os.path.isabs(centers_csv) else os.path.join(HERE, centers_csv)
    db_path = db if os.path.isabs(db) else os.path.join(HERE, db)

    ids = read_center_ids(csv_path)
    meta = load_center_meta()
    reports = [only] if isinstance(only, str) else list(only) if only else list(REPORTS)

    if session is None:
        session = new_session(start, end, ids, reports)
        resumed = False
    else:
        start, end = session["start"], session["end"]
        # a resume finishes what that session set out to do, whatever the CSV says today
        ids = session.get("centers") or ids
        if not only:
            reports = [r for r in session.get("reports", []) if r in REPORTS] or reports
        session["status"] = "running"
        _save_session(session)
        resumed = True
    sid, folder = session["session_id"], session["folder"]

    conn = sqlite3.connect(db_path)
    init_db(conn)
    save_session_row(conn, session)

    http, _ = C.load_session()
    mb = requests.Session()
    mb.cookies.set("metabase.DEVICE", str(uuid.uuid4()))

    console.print(Panel.fit(
        f"[bold]Curefit scraper[/bold]  session [magenta]{sid}[/magenta]"
        f"{' (resumed)' if resumed else ''}\n"
        f"period [cyan]{start} → {end}[/cyan]\n"
        f"centers [cyan]{len(ids)}[/cyan]   reports [cyan]{len(reports)}[/cyan]\n"
        f"folder [dim]{folder}[/dim]\ndb [dim]{db_path}[/dim]", border_style="cyan"))

    stats, done = {}, {}
    for rep_name in reports:
        done[rep_name] = set() if refresh else {r[0] for r in conn.execute(
            "SELECT center_id FROM scrape_log WHERE session_id=? AND report=? AND start_date=? "
            "AND end_date=?", (sid, rep_name, start, end))}
        stats[rep_name] = {"requested": len(ids), "already_done": len(done[rep_name] & set(ids)),
                           "ok": 0, "empty": [], "failed": [], "capped": [], "skipped": False}

    # reports sharing a dashboard share one signed token per center
    groups = {}
    for rep_name in reports:
        groups.setdefault(REPORTS[rep_name]["dashboard"], []).append(rep_name)

    total = sum(len([c for c in ids if c not in done[r]]) for r in reports)
    if status is not None:
        status.update(state="running", session=sid, folder=folder, done=0, total=total,
                      desc="", error=None, stop=False, pause=False, log=[], cells={},
                      active=[], started_at=time.time(), workers=workers)
    fail_streak = {r: 0 for r in reports}
    lock = threading.Lock()  # guards stats/flags/progress when several centers run at once
    flags = {"auth_failures": 0, "aborted": False, "stopped": False}

    def note(msg):
        if status is not None:
            with lock:
                status["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")
                del status["log"][:-200]

    def advance():
        with lock:
            progress.advance(task)
            if status is not None:
                status["done"] += 1

    def mark(cid, rep_name, state):
        """Live cell state for the admin's monitor: failed, or in-flight (active)."""
        if status is None:
            return
        with lock:
            key = f"{cid}|{rep_name}"
            if state == "active":
                status["active"].append(key)
            else:
                if key in status["active"]:
                    status["active"].remove(key)
                if state == "failed":
                    status["cells"][key] = "failed"

    def process_center(cid):
        """Every pending section of one center, on this thread's own connection + session."""
        name, code = meta.get(cid, ("", ""))
        tconn = sqlite3.connect(db_path, timeout=60)
        tmb = requests.Session()
        tmb.cookies.set("metabase.DEVICE", str(uuid.uuid4()))
        try:
            for reps in groups.values():
                token = None
                for rep_name in [r for r in reps if cid not in done[r]]:
                    if flags["aborted"]:
                        return
                    if status is not None and status.get("stop"):
                        with lock:
                            flags["aborted"] = flags["stopped"] = True
                        return
                    while status is not None and status.get("pause") and not status.get("stop"):
                        time.sleep(1)  # paused from the admin: hold here until resumed or stopped
                    if status is not None and status.get("stop"):
                        with lock:
                            flags["aborted"] = flags["stopped"] = True
                        return
                    st = stats[rep_name]
                    if st["skipped"]:
                        advance()
                        continue
                    rep = REPORTS[rep_name]
                    now = time.strftime("%Y-%m-%dT%H:%M:%S")
                    progress.update(task, description=f"{rep_name} · center {cid} "
                                                      f"[dim]{name or code}[/dim]")
                    if status is not None:
                        status["desc"] = f"{rep_name} · center {cid} {name or code}"
                    mark(cid, rep_name, "active")
                    try:
                        rows, cols = [], []
                        for attempt in (1, 2):  # an empty result gets one retry, fresh token
                            if token is None:
                                token = get_token(http, rep, cid, start, end)
                                warm_up(tmb, token)
                            rows, cols = get_rows(tmb, rep, token, start, end)
                            if rows:
                                break
                            log.info("empty_result_retry", report=rep_name, center_id=cid,
                                     attempt=attempt)
                            token = None
                            polite_sleep()
                        if not rows:
                            # empty after a retry on a fresh token: recorded as done (0 rows)
                            # for this session only; a new session fetches it again
                            tconn.execute("INSERT OR REPLACE INTO scrape_log VALUES (?,?,?,?,?,?,?)",
                                          (rep_name, cid, start, end, 0, now, sid))
                            tconn.commit()
                            with lock:
                                st["empty"].append(cid)
                            log.warning("center_empty", report=rep_name, center_id=cid,
                                        center=name or code)
                            note(f"empty  {rep_name} · {cid} {name or code}")
                        else:
                            if len(rows) >= ROW_CAP:
                                with lock:
                                    st["capped"].append(cid)
                                log.warning("possible_truncation", report=rep_name,
                                            center_id=cid, rows=len(rows), cap=ROW_CAP)
                            if rep_name in STORE:
                                STORE[rep_name](tconn, cid, name, code, start, end, rows, now, sid)
                            else:
                                store_generic(tconn, rep_name, cols, cid, name, code,
                                              start, end, rows, now, sid)
                            write_center_file(folder, cid, name, code, rep_name, cols, rows)
                            tconn.execute("INSERT OR REPLACE INTO scrape_log VALUES (?,?,?,?,?,?,?)",
                                          (rep_name, cid, start, end, len(rows), now, sid))
                            tconn.commit()
                            with lock:
                                st["ok"] += 1
                            log.info("center_ok", report=rep_name, center_id=cid,
                                     center=name or code, rows=len(rows))
                            note(f"ok     {rep_name} · {cid} {name or code} · {len(rows)} rows")
                        with lock:
                            fail_streak[rep_name] = 0
                            flags["auth_failures"] = 0
                    except AuthError as e:
                        tconn.rollback()
                        with lock:
                            st["failed"].append(cid)
                            flags["auth_failures"] += 1
                            if flags["auth_failures"] >= AUTH_FAIL_LIMIT:
                                flags["aborted"] = True
                        log.error("auth_failed", report=rep_name, center_id=cid, error=str(e))
                        note(f"AUTH   {rep_name} · {cid} {e}")
                        mark(cid, rep_name, "failed")
                    except Exception as e:
                        tconn.rollback()
                        token = None
                        with lock:
                            st["failed"].append(cid)
                            fail_streak[rep_name] += 1
                            skip = fail_streak[rep_name] >= REPORT_FAIL_LIMIT and st["ok"] == 0
                            if skip:
                                st["skipped"] = True
                        log.error("center_failed", report=rep_name, center_id=cid, error=str(e))
                        note(f"FAILED {rep_name} · {cid} {name or code} · {e}")
                        mark(cid, rep_name, "failed")
                        if skip:
                            log.error("report_skipped", report=rep_name,
                                      reason=f"{REPORT_FAIL_LIMIT} failures in a row, none succeeded")
                    mark(cid, rep_name, "done")
                    advance()
                    if flags["aborted"]:
                        return
                    polite_sleep()
        finally:
            tconn.close()
            tmb.close()

    progress = Progress(SpinnerColumn(), BarColumn(), MofNCompleteColumn(),
                        TextColumn("{task.description}"), TimeElapsedColumn(), console=console)
    with progress:
        task = progress.add_task("", total=total)
        if workers <= 1:
            for cid in ids:
                if flags["aborted"]:
                    break
                process_center(cid)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for fut in [pool.submit(process_center, cid) for cid in ids]:
                    fut.result()

    aborted, stopped = flags["aborted"], flags["stopped"]
    if stopped:
        console.print("[yellow]Stopped by user — continue this session later to finish.[/yellow]")
    elif aborted:
        console.print("[bold red]Session rejected (401/403) — stopping. Log in again "
                      "(python run.py) and continue this session; saved centers are kept."
                      "[/bold red]")

    totals = {k: sum(len(v[k]) for v in stats.values()) for k in ("empty", "failed", "capped")}
    totals["ok"] = sum(v["ok"] for v in stats.values())
    log.info("scrape_done", session=sid, ok=totals["ok"], failed=totals["failed"],
             empty=totals["empty"], possibly_truncated=totals["capped"], aborted=aborted)

    written, analysis = export_reports(conn, start, end, sid, folder)
    cov_path = os.path.join(folder, "coverage.csv")
    cov_rows = _write_coverage(stats, cov_path)
    written.append(cov_path)
    written.extend(merge_session(folder, meta))
    print_analysis(analysis, start, end)

    # complete = every requested center/section is saved in this session
    saved = {r: {x[0] for x in conn.execute(
        "SELECT center_id FROM scrape_log WHERE session_id=? AND report=? AND start_date=? "
        "AND end_date=?", (sid, r, start, end))} for r in reports}
    missing = {r: [c for c in ids if c not in saved[r]] for r in reports}
    session["status"] = "complete" if not any(missing.values()) else "incomplete"
    session["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    session["missing"] = {r: m for r, m in missing.items() if m}
    _save_session(session)
    cov_cols = ["report", "requested", "already_done", "scraped", "empty", "failed",
                "possibly_truncated", "skipped_after_failures", "empty_center_ids",
                "failed_center_ids"]
    save_session_row(conn, session, [dict(zip(cov_cols, r)) for r in cov_rows])

    cov = Table(title="Coverage", header_style="bold cyan")
    for h in ("Report", "Centers", "Already done", "Scraped", "Empty", "Failed", "Capped", "Skipped"):
        cov.add_column(h, justify="right" if h != "Report" else "left")
    for r in cov_rows:
        cov.add_row(str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), str(r[5]),
                    str(r[6]), "yes" if r[7] else "")
    console.print(cov)
    for rep_name, st in stats.items():
        if st["capped"]:
            console.print(f"[red]{rep_name}: {len(st['capped'])} result(s) hit the {ROW_CAP}-row "
                          f"cap, may be incomplete (centers {', '.join(st['capped'])})[/red]")

    files_tbl = Table(title="Period files", header_style="bold green", show_header=False)
    for pth in written:
        files_tbl.add_row(os.path.relpath(pth, folder))
    console.print(files_tbl)
    colour = "green" if session["status"] == "complete" else "yellow"
    console.print(f"Session [magenta]{sid}[/magenta] is [{colour}]{session['status']}[/{colour}] "
                  f"→ {folder}")
    if status is not None:
        status.update(state="done", desc="", session_status=session["status"])
    conn.close()
    return session


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--centers-csv", default=CENTERS_CSV)
    ap.add_argument("--start", default=START_DATE)
    ap.add_argument("--end", default=END_DATE)
    ap.add_argument("--db", default=DB_FILE)
    ap.add_argument("--only", choices=list(REPORTS), help="scrape just one report")
    ap.add_argument("--session", help="resume this session id (default: start a new session)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch everything, even what the session already saved")
    ap.add_argument("--merge", metavar="SESSION_ID",
                    help="only rebuild merged/ (one file per report) for this session")
    args = ap.parse_args()
    if args.merge:
        info = find_session(args.merge)
        if info is None:
            raise SystemExit(f"No session '{args.merge}' under {RUNS_DIR}")
        for pth in merge_session(info["folder"]):
            console.print(os.path.relpath(pth, info["folder"]))
        return
    session = None
    if args.session:
        session = find_session(args.session)
        if session is None:
            raise SystemExit(f"No session '{args.session}' under {RUNS_DIR}")
    run_scrape(args.start, args.end, args.centers_csv, args.db, args.only, args.refresh, session)


if __name__ == "__main__":
    main()
