"""
Curefit / Kronos multi-report scraper -> local SQLite + versioned CSV reports.

Two reports are scraped per center (config-driven, add more in REPORTS):
  - sales : member-level sales list   (dashboard 2536 / dashcard 19822 / card 39684)
  - perf  : center KPI metrics        (dashboard 1750 / dashcard 13395 / card 24755)

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
import sqlite3
import time
import uuid
import urllib.parse

import requests
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
}
# ===================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
CENTERS_JSON = os.path.join(HERE, "centers.json")


def polite_sleep():
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


def load_center_meta():
    meta = {}
    if os.path.exists(CENTERS_JSON):
        data = json.load(open(CENTERS_JSON))
        for c in data.get("centers", []):
            meta[str(c["id"])] = (c.get("name", "").strip(), c.get("centerCode", ""))
    return meta


def read_center_ids(path):
    ids = []
    with open(path, newline="") as f:
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


# ---------------------------- DB ----------------------------
def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sales (
        center_id TEXT, center_name TEXT, center_code TEXT,
        start_date TEXT, end_date TEXT,
        user_id TEXT, name TEXT, phone TEXT, pack_name TEXT, member_end_date TEXT,
        active_last_30d TEXT, renewed TEXT, revenue REAL, scraped_at TEXT,
        PRIMARY KEY (center_id, start_date, end_date, user_id, pack_name)
    );
    CREATE TABLE IF NOT EXISTS perf_metrics (
        center_id TEXT, center_name TEXT, center_code TEXT,
        start_date TEXT, end_date TEXT,
        metric_date TEXT, metric TEXT, value REAL, scraped_at TEXT,
        PRIMARY KEY (center_id, start_date, end_date, metric_date, metric)
    );
    CREATE TABLE IF NOT EXISTS scrape_log (
        report TEXT, center_id TEXT, start_date TEXT, end_date TEXT,
        row_count INTEGER, scraped_at TEXT,
        PRIMARY KEY (report, center_id, start_date, end_date)
    );
    """)
    conn.commit()


# ------------------------- HTTP core -------------------------
def _with_retry(fn, what):
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except Exception as e:
            last = e
            wait = BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 1)
            print(f"    ! {what} failed ({e}); retry {attempt+1}/{MAX_RETRIES} in {wait:.1f}s")
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


def get_rows(mb, rep, token, start, end, tries=6):
    params = urllib.parse.quote(json.dumps(rep["data_params"](start, end)))
    url = (f"https://metabase.curefit.co/api/embed/dashboard/{token}"
           f"/dashcard/{rep['dashcard']}/card/{rep['card']}?parameters={params}")
    hdr = {
        "accept": "application/json",
        "x-metabase-client": "embedding-iframe",
        "x-metabase-embedded": "true",
        "x-metabase-guest-embed": "true",
        "referer": f"https://metabase.curefit.co/embed/dashboard/{token}",
        "user-agent": C.UA,
    }

    def do():
        j = {}
        for _ in range(tries):
            r = mb.get(url, headers=hdr, timeout=TIMEOUT)
            r.raise_for_status()
            j = r.json()
            if j.get("status") == "completed":
                return j["data"]["rows"]
            time.sleep(1.5)  # metabase still computing
        return j.get("data", {}).get("rows", [])
    return _with_retry(do, "data")


# ------------------------- persistence -------------------------
def store_sales(conn, cid, name, code, start, end, rows, now):
    for r in rows:  # [user_id,name,phone,pack,end,active,renewed,revenue]
        conn.execute("""INSERT OR REPLACE INTO sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (cid, name, code, start, end, str(r[0]), r[1], r[2], r[3], r[4],
                      r[5], r[6], r[7], now))


def store_perf(conn, cid, name, code, start, end, rows, now):
    for r in rows:  # [date, metric, value]
        conn.execute("""INSERT OR REPLACE INTO perf_metrics VALUES (?,?,?,?,?,?,?,?,?)""",
                     (cid, name, code, start, end, r[0], (r[1] or "").strip(), r[2], now))


STORE = {"sales": store_sales, "perf": store_perf}


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


def export_reports(conn, start, end):
    ts = time.strftime("%Y%m%d-%H%M%S")
    written = []

    # 1) sales member-level report
    cols = ["center_id", "center_name", "center_code", "start_date", "end_date",
            "user_id", "name", "phone", "pack_name", "member_end_date",
            "active_last_30d", "renewed", "revenue", "scraped_at"]
    rows = conn.execute(
        f"SELECT {','.join(cols)} FROM sales WHERE start_date=? AND end_date=? "
        f"ORDER BY CAST(center_id AS INT), name", (start, end)).fetchall()
    p = versioned_path(os.path.join(HERE, f"sales_report_{start}_{end}_{ts}"))
    _write_csv(p, cols, rows); written.append(p)

    # 2) perf long report
    pcols = ["center_id", "center_name", "center_code", "start_date", "end_date",
             "metric_date", "metric", "value", "scraped_at"]
    prows = conn.execute(
        f"SELECT {','.join(pcols)} FROM perf_metrics WHERE start_date=? AND end_date=? "
        f"ORDER BY CAST(center_id AS INT), metric", (start, end)).fetchall()
    p = versioned_path(os.path.join(HERE, f"perf_long_{start}_{end}_{ts}"))
    _write_csv(p, pcols, prows); written.append(p)

    # 3) perf pivoted wide (one row per center, metrics as columns)
    metrics = [m[0] for m in conn.execute(
        "SELECT DISTINCT metric FROM perf_metrics WHERE start_date=? AND end_date=? ORDER BY metric",
        (start, end))]
    centers = conn.execute(
        "SELECT DISTINCT center_id, center_name FROM perf_metrics "
        "WHERE start_date=? AND end_date=? ORDER BY CAST(center_id AS INT)", (start, end))
    pivot_rows = []
    for cid, cname in centers.fetchall():
        vals = dict(conn.execute(
            "SELECT metric, value FROM perf_metrics WHERE center_id=? AND start_date=? AND end_date=?",
            (cid, start, end)).fetchall())
        pivot_rows.append([cid, cname] + [vals.get(m) for m in metrics])
    p = versioned_path(os.path.join(HERE, f"perf_pivot_{start}_{end}_{ts}"))
    _write_csv(p, ["center_id", "center_name"] + metrics, pivot_rows); written.append(p)

    # 4) analysis: revenue summary + new/repeat split + combined
    analysis = build_analysis(conn, start, end, metrics)
    p = versioned_path(os.path.join(HERE, f"analysis_{start}_{end}_{ts}"))
    _write_csv(p, analysis["header"], analysis["rows"]); written.append(p)

    # 5) standalone HTML dashboard (data embedded, no server needed)
    p = versioned_path(os.path.join(HERE, f"dashboard_{start}_{end}_{ts}"), ".html")
    _write_html(p, start, end, analysis)
    written.append(p)

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


def build_analysis(conn, start, end, metrics):
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
            SELECT center_id, center_name FROM sales WHERE start_date=? AND end_date=?
            UNION SELECT center_id, center_name FROM perf_metrics WHERE start_date=? AND end_date=?
        ) GROUP BY center_id ORDER BY CAST(center_id AS INT)
    """, (start, end, start, end)).fetchall()

    for cid, cname in centers:
        sc = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(revenue),0), "
            "SUM(CASE WHEN renewed='Yes' THEN 1 ELSE 0 END) "
            "FROM sales WHERE center_id=? AND start_date=? AND end_date=?",
            (cid, start, end)).fetchone()
        count, sales_rev, renewals = sc[0], sc[1], sc[2] or 0
        rr = round(100.0 * renewals / count, 1) if count else 0.0

        def pv(metric):
            if not metric:
                return None
            row = conn.execute(
                "SELECT SUM(value) FROM perf_metrics WHERE center_id=? AND metric=? "
                "AND start_date=? AND end_date=?", (cid, metric, start, end)).fetchone()
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


# ----------------------------- runner -----------------------------
def run_scrape(start, end, centers_csv=CENTERS_CSV, db=DB_FILE, only=None, refresh=False):
    """Scrape all configured reports for the centers in the CSV. Callable from run.py."""
    csv_path = centers_csv if os.path.isabs(centers_csv) else os.path.join(HERE, centers_csv)
    db_path = db if os.path.isabs(db) else os.path.join(HERE, db)

    ids = read_center_ids(csv_path)
    meta = load_center_meta()
    reports = [only] if only else list(REPORTS)

    conn = sqlite3.connect(db_path)
    init_db(conn)

    session, _ = C.load_session()
    mb = requests.Session()
    mb.cookies.set("metabase.DEVICE", str(uuid.uuid4()))

    console.print(Panel.fit(
        f"[bold]Curefit scraper[/bold]\n"
        f"period [cyan]{start} → {end}[/cyan]\n"
        f"centers [cyan]{len(ids)}[/cyan]   reports [cyan]{', '.join(reports)}[/cyan]\n"
        f"db [dim]{db_path}[/dim]", border_style="cyan"))

    ok_count = fail_count = 0
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.fields[report]}[/bold]"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("{task.description}"),
        TimeElapsedColumn(),
        console=console,
    )
    with progress:
        for rep_name in reports:
            rep = REPORTS[rep_name]
            done = set()
            if not refresh:
                done = {r[0] for r in conn.execute(
                    "SELECT center_id FROM scrape_log WHERE report=? AND start_date=? AND end_date=?",
                    (rep_name, start, end))}
            todo = [c for c in ids if c not in done]
            log.info("report_start", report=rep_name, to_scrape=len(todo), skipped=len(done))
            task = progress.add_task("", report=rep_name, total=len(todo))

            for cid in todo:
                name, code = meta.get(cid, ("", ""))
                now = time.strftime("%Y-%m-%dT%H:%M:%S")
                progress.update(task, description=f"center {cid} [dim]{name or code}[/dim]")
                try:
                    token = get_token(session, rep, cid, start, end)
                    rows = get_rows(mb, rep, token, start, end)
                    STORE[rep_name](conn, cid, name, code, start, end, rows, now)
                    conn.execute("INSERT OR REPLACE INTO scrape_log VALUES (?,?,?,?,?,?)",
                                 (rep_name, cid, start, end, len(rows), now))
                    conn.commit()
                    ok_count += 1
                    log.info("center_ok", report=rep_name, center_id=cid,
                             center=name or code, rows=len(rows))
                except Exception as e:
                    fail_count += 1
                    log.error("center_failed", report=rep_name, center_id=cid, error=str(e))
                progress.advance(task)
                polite_sleep()

    log.info("scrape_done", ok=ok_count, failed=fail_count)
    written, analysis = export_reports(conn, start, end)
    print_analysis(analysis, start, end)

    files_tbl = Table(title="Files written", header_style="bold green", show_header=False)
    for p in written:
        files_tbl.add_row(os.path.basename(p))
    console.print(files_tbl)
    conn.close()
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--centers-csv", default=CENTERS_CSV)
    ap.add_argument("--start", default=START_DATE)
    ap.add_argument("--end", default=END_DATE)
    ap.add_argument("--db", default=DB_FILE)
    ap.add_argument("--only", choices=list(REPORTS), help="scrape just one report")
    ap.add_argument("--refresh", action="store_true", help="re-scrape even if logged")
    args = ap.parse_args()
    run_scrape(args.start, args.end, args.centers_csv, args.db, args.only, args.refresh)


if __name__ == "__main__":
    main()
