"""
Local admin for the Curefit scraper — one page, this PC only.

  Sessions   every run with status, per-report coverage, missing/empty/failed centers,
             downloads of the merged files, continue / merge actions
  Run        log in with the emailed OTP, pick a period, start / continue / stop a scrape
  Analytics  the sales + perf charts for any session

    python admin_server.py
    python admin_server.py --db curefit.db --port 8000

It listens on 127.0.0.1 only. Because it can start scrapes, every change (POST) needs a
per-start token that only this page knows, and requests from other sites are refused.
Standard library only (plus the scraper's own dependencies).
"""
import argparse
import datetime as dt
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import curefit_login as C
import scrape_sales as S

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "curefit.db")
PORT = 8000
TOKEN = secrets.token_urlsafe(24)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

JOB = {"state": "idle", "session": None, "folder": None, "done": 0, "total": 0, "desc": "",
       "log": [], "stop": False, "error": None}
_job_lock = threading.Lock()
_auth = {"at": 0.0, "ok": False}


class ApiError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def db():
    return sqlite3.connect(DB_PATH)


# ------------------------------ data ------------------------------
def list_periods():
    """Sessions that have scraped data (for the Analytics picker)."""
    conn = db()
    try:
        rows = conn.execute("""
            SELECT session_id, start_date, end_date, COUNT(DISTINCT center_id), MAX(scraped_at)
            FROM scrape_log WHERE row_count>0
            GROUP BY session_id, start_date, end_date ORDER BY MAX(scraped_at) DESC
        """).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [{"session": r[0], "start": r[1], "end": r[2], "centers": r[3], "last": r[4]}
            for r in rows]


def sessions_payload():
    """Every session with status, coverage and what is still missing (+ legacy periods)."""
    conn = db()
    try:
        rows = conn.execute("""
            SELECT session_id, start_date, end_date, status, created_at, finished_at, folder,
                   centers, reports, coverage, missing FROM sessions ORDER BY created_at DESC
        """).fetchall()
        legacy = conn.execute("""
            SELECT start_date, end_date, COUNT(DISTINCT center_id), MAX(scraped_at)
            FROM scrape_log WHERE session_id='legacy' AND row_count>0
            GROUP BY start_date, end_date ORDER BY MAX(scraped_at) DESC
        """).fetchall()
    except sqlite3.OperationalError:
        rows, legacy = [], []
    conn.close()
    out = []
    for r in rows:
        missing = json.loads(r[10] or "{}")
        out.append({"session": r[0], "start": r[1], "end": r[2], "status": r[3],
                    "created_at": r[4], "finished_at": r[5], "folder": r[6],
                    "centers": len(json.loads(r[7] or "[]")),
                    "reports": json.loads(r[8] or "[]"),
                    "coverage": json.loads(r[9] or "null"), "missing": missing,
                    "missing_count": sum(len(v) for v in missing.values())})
    for start, end, n, last in legacy:
        out.append({"session": "legacy", "start": start, "end": end, "status": "legacy",
                    "created_at": last, "finished_at": last, "folder": None, "centers": n,
                    "reports": ["sales", "perf"], "coverage": None, "missing": {},
                    "missing_count": 0})
    return out


def _session_folder(sid):
    info = S.find_session(sid)
    if not info:
        raise ApiError(f"Unknown session '{sid}'", 404)
    return info


def session_files(sid):
    info = _session_folder(sid)
    folder, files = info["folder"], []
    for sub in ("", "merged"):
        d = os.path.join(folder, sub)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                p = os.path.join(d, f)
                if os.path.isfile(p):
                    files.append({"path": f"{sub}/{f}" if sub else f, "size": os.path.getsize(p)})
    centers = os.path.join(folder, "centers")
    n = len(os.listdir(centers)) if os.path.isdir(centers) else 0
    return {"folder": folder, "files": files, "center_folders": n}


def analysis_payload(sid, start, end):
    conn = db()
    metrics = [r[0] for r in conn.execute(
        "SELECT DISTINCT metric FROM perf_metrics WHERE session_id=? AND start_date=? "
        "AND end_date=? ORDER BY metric", (sid, start, end))]
    analysis = S.build_analysis(conn, start, end, metrics, sid)
    rows = analysis["rows"]

    extra = {}
    for cid, cnt, act, rev in conn.execute(
        """SELECT center_id, COUNT(*),
                  SUM(CASE WHEN active_last_30d='Yes' THEN 1 ELSE 0 END),
                  COALESCE(SUM(revenue),0)
           FROM sales WHERE session_id=? AND start_date=? AND end_date=?
           GROUP BY center_id""", (sid, start, end)):
        extra[str(cid)] = {
            "active_count": act or 0,
            "active_rate": round(100.0 * (act or 0) / cnt, 1) if cnt else 0,
            "avg_ticket": round(rev / cnt, 0) if cnt else 0,
        }

    packs = [{"pack": p, "count": c, "revenue": round(r or 0, 0)}
             for p, c, r in conn.execute(
                 """SELECT pack_name, COUNT(*), SUM(revenue)
                    FROM sales WHERE session_id=? AND start_date=? AND end_date=?
                    GROUP BY pack_name ORDER BY COUNT(*) DESC""", (sid, start, end))]

    sc = sum((r[2] or 0) for r in rows)
    sr = sum((r[3] or 0) for r in rows)
    ac = sum(e["active_count"] for e in extra.values())
    totals = {
        "centers": len(rows), "sales_count": sc, "sales_revenue": sr,
        "total_revenue": sum((r[6] or 0) for r in rows),
        "new_revenue": sum((r[7] or 0) for r in rows),
        "repeat_revenue": sum((r[8] or 0) for r in rows),
        "avg_ticket": round(sr / sc, 0) if sc else 0,
        "active_rate": round(100.0 * ac / sc, 1) if sc else 0,
    }
    conn.close()
    return {"header": analysis["header"], "rows": rows, "extra": extra,
            "packs": packs, "totals": totals, "start": start, "end": end}


def sales_detail(sid, start, end, center_id):
    conn = db()
    cols = ["user_id", "name", "phone", "pack_name", "member_end_date",
            "active_last_30d", "renewed", "revenue"]
    rows = conn.execute(
        f"SELECT {','.join(cols)} FROM sales WHERE session_id=? AND start_date=? AND end_date=? "
        f"AND center_id=? ORDER BY revenue DESC NULLS LAST, name",
        (sid, start, end, str(center_id))).fetchall()
    conn.close()
    return {"header": cols, "rows": rows}


# ------------------------------ login + jobs ------------------------------
def logged_in(fresh=False):
    if fresh or time.time() - _auth["at"] > 30:
        _auth.update(at=time.time(), ok=C.session_ok())
    return _auth["ok"]


def send_otp():
    r = C.send_otp()
    return {"ok": r.status_code == 200, "email": C.EMAIL}


def verify_otp(otp):
    otp = str(otp or "").strip()
    if not re.fullmatch(r"\d{4,8}", otp):
        raise ApiError("Enter the numeric OTP from the email")
    r = C.validate_otp(otp)
    try:
        ok = r.status_code == 200 and "token" in r.json()
    except ValueError:
        ok = False
    if ok:
        sr = C.establish_session()
        ok = sr is not None and sr.status_code == 200
    _auth.update(at=time.time(), ok=bool(ok) and C.session_ok())
    return {"ok": _auth["ok"]}


def _run_job(start, end, session, only):
    try:
        S.run_scrape(start, end, db=DB_PATH, only=only, session=session, status=JOB)
    except Exception as e:  # keep the server alive, show the reason in the UI
        JOB.update(state="error", error=str(e))


def start_job(body):
    start, end = body.get("start", ""), body.get("end", "")
    session = None
    if body.get("session"):
        session = _session_folder(body["session"])
        start, end = session["start"], session["end"]
    for d in (start, end):
        if not DATE_RE.match(d or ""):
            raise ApiError("Dates must be YYYY-MM-DD")
        try:
            dt.date.fromisoformat(d)
        except ValueError:
            raise ApiError(f"Not a valid date: {d}")
    if start > end:
        raise ApiError("Start date is after the end date")
    only = body.get("only") or None
    if only is not None:
        if not isinstance(only, list) or not all(o in S.REPORTS for o in only):
            raise ApiError("Unknown report in selection")
    if not logged_in(fresh=True):
        raise ApiError("Not logged in — send and verify the OTP first", 401)
    with _job_lock:
        if JOB["state"] == "running":
            raise ApiError("A scrape is already running", 409)
        JOB.update(state="running", session=None, folder=None, done=0, total=0, desc="starting…",
                   log=[], stop=False, error=None)
        threading.Thread(target=_run_job, args=(start, end, session, only), daemon=True).start()
    return {"ok": True}


def job_payload():
    j = {k: v for k, v in JOB.items() if k != "log"}
    j["log"] = JOB["log"][-60:]
    return j


def config_payload():
    today = dt.date.today()
    first_prev = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    last_prev = today.replace(day=1) - dt.timedelta(days=1)
    try:
        n = len(S.read_center_ids(os.path.join(HERE, S.CENTERS_CSV)))
    except OSError:
        n = 0
    return {"reports": list(S.REPORTS), "centers": n, "email": C.EMAIL,
            "default_start": first_prev.isoformat(), "default_end": last_prev.isoformat()}


# ------------------------------ http ------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _send(self, body, ctype="application/json", code=200, headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, default=str), code=code)

    def _guard(self, post):
        """Only this machine, and for changes only this page (DNS-rebinding / CSRF defence)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("localhost", "127.0.0.1"):
            raise ApiError("Forbidden host", 403)
        if post:
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
                raise ApiError("Forbidden origin", 403)
            if not secrets.compare_digest(self.headers.get("X-Admin-Token", ""), TOKEN):
                raise ApiError("Missing or wrong admin token (reload the page)", 403)

    def _dispatch(self, post):
        try:
            self._guard(post)
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            body = {}
            if post:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            self._route(u.path, q, body, post)
        except ApiError as e:
            self._json({"error": str(e)}, e.code)
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            self._json({"error": f"Bad request: {e}"}, 400)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_GET(self):
        self._dispatch(False)

    def do_POST(self):
        self._dispatch(True)

    def _route(self, path, q, body, post):
        if not post:
            if path in ("/", "/index.html"):
                return self._send(PAGE.replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
            if path == "/api/config":
                return self._json(config_payload())
            if path == "/api/status":
                return self._json({"logged_in": logged_in(q.get("fresh") == "1"),
                                   "email": C.EMAIL, "job": job_payload()})
            if path == "/api/job":
                return self._json(job_payload())
            if path == "/api/sessions":
                return self._json(sessions_payload())
            if path == "/api/session_files":
                return self._json(session_files(q["session"]))
            if path == "/api/unfinished":
                u = S.find_unfinished(q["start"], q["end"])
                return self._json({"session": u["session_id"] if u else None,
                                   "created_at": u["created_at"] if u else None})
            if path == "/api/periods":
                return self._json(list_periods())
            if path == "/api/analysis":
                return self._json(analysis_payload(q["session"], q["start"], q["end"]))
            if path == "/api/sales":
                return self._json(sales_detail(q["session"], q["start"], q["end"], q["center_id"]))
            if path == "/download":
                return self._download(q["session"], q["path"])
        else:
            if path == "/api/login/send":
                return self._json(send_otp())
            if path == "/api/login/verify":
                return self._json(verify_otp(body.get("otp")))
            if path == "/api/run":
                return self._json(start_job(body))
            if path == "/api/stop":
                JOB["stop"] = True
                return self._json({"ok": True})
            if path == "/api/merge":
                info = _session_folder(body["session"])
                files = S.merge_session(info["folder"])
                return self._json({"ok": True, "files": [os.path.basename(f) for f in files]})
        raise ApiError("Not found", 404)

    def _download(self, sid, rel):
        info = _session_folder(sid)
        root = os.path.realpath(info["folder"])
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            raise ApiError("File not found", 404)
        with open(full, "rb") as f:
            data = f.read()
        ctype = "text/csv; charset=utf-8" if full.endswith(".csv") else \
            "application/octet-stream"
        name = os.path.basename(full)
        self._send(data, ctype, headers={"Content-Disposition": f'attachment; filename="{name}"'})


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Curefit Admin</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
 :root{--bg:#0f1115;--panel:#161a22;--line:#262b36;--ink:#e6e6e6;--mute:#8b93a1;--accent:#4f8cff;
       --ok:#33c48d;--warn:#f5a623;--bad:#e5567b}
 *{box-sizing:border-box}
 body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:var(--bg);color:var(--ink)}
 header{padding:14px 28px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;
        align-items:center;gap:22px;flex-wrap:wrap}
 h1{margin:0;font-size:18px} h2{font-size:13px;margin:0 0 12px;color:#c7cdd8;text-transform:uppercase;letter-spacing:.04em}
 nav{display:flex;gap:6px} nav button{background:none;border:1px solid transparent;color:var(--mute);
   padding:7px 14px;border-radius:7px;font-size:14px;cursor:pointer}
 nav button.on{background:#1c212b;color:var(--ink);border-color:#333a48}
 select,button,input{background:#1c212b;color:var(--ink);border:1px solid #333a48;border-radius:7px;
        padding:7px 10px;font-size:13px;font-family:inherit}
 button{cursor:pointer} button:hover:not(:disabled){background:#252c39} button:disabled{opacity:.5;cursor:default}
 button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
 button.danger{border-color:var(--bad);color:var(--bad)}
 .wrap{padding:22px 28px;max-width:1250px;margin:0 auto}
 .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:20px}
 .cards{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:20px}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 18px;min-width:150px}
 .card .lbl{color:var(--mute);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
 .card .val{font-size:22px;font-weight:600;margin-top:6px}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:7px 9px;text-align:right;border-bottom:1px solid var(--line)}
 th:first-child,td:first-child,.l{text-align:left}
 th{color:var(--mute);font-weight:600;position:sticky;top:0;background:var(--panel)}
 tbody tr.click{cursor:pointer} tbody tr:hover td{background:#1c212b} tr.sel td{background:#20304a}
 .badge{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;border:1px solid}
 .complete{color:var(--ok);border-color:var(--ok)} .incomplete{color:var(--warn);border-color:var(--warn)}
 .running{color:var(--accent);border-color:var(--accent)} .legacy{color:var(--mute);border-color:#444}
 .muted{color:var(--mute)} .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
 .bar{height:10px;background:#1c212b;border-radius:99px;overflow:hidden;margin:10px 0}
 .bar>div{height:100%;background:var(--accent);width:0}
 pre{background:#0b0d11;border:1px solid var(--line);border-radius:8px;padding:10px;font-size:12px;
     max-height:260px;overflow:auto;margin:0;white-space:pre-wrap}
 .checks{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:4px 14px}
 .checks label{font-size:13px;color:#c7cdd8} .hide{display:none}
 #toast{position:fixed;right:20px;bottom:20px;background:#1c212b;border:1px solid #333a48;border-radius:8px;
        padding:10px 14px;font-size:13px;max-width:380px;display:none}
 @media(max-width:900px){.grid{grid-template-columns:1fr}.wrap{padding:16px}}
</style></head><body>
<header>
  <h1>Curefit Admin</h1>
  <nav id="tabs">
    <button data-tab="sessions" class="on">Sessions</button>
    <button data-tab="run">Run</button>
    <button data-tab="analytics">Analytics</button>
  </nav>
  <span style="flex:1"></span>
  <span id="loginBadge" class="badge legacy">checking login…</span>
</header>
<div class="wrap">

<section id="tab-sessions">
  <div class="panel"><h2>Sessions <span class="muted" style="text-transform:none">— newest first</span></h2>
    <div style="overflow:auto"><table id="sessTbl"></table></div></div>
  <div class="panel hide" id="sessDetail"></div>
</section>

<section id="tab-run" class="hide">
  <div class="panel"><h2>1 · Login</h2>
    <div class="row"><span id="loginTxt"></span></div>
    <div class="row">
      <button id="sendOtp">Send OTP to <span id="emailTxt"></span></button>
      <input id="otp" inputmode="numeric" autocomplete="one-time-code" placeholder="OTP" size="8">
      <button id="verifyOtp" class="primary">Verify</button>
    </div>
  </div>
  <div class="panel"><h2>2 · Period & reports</h2>
    <div class="row">
      <label>Start <input type="date" id="start"></label>
      <label>End <input type="date" id="end"></label>
      <span class="muted" id="centerCount"></span>
    </div>
    <div class="row" id="resumeRow"></div>
    <div class="checks" id="reports"></div>
    <div class="row" style="margin-top:10px"><button id="allRep">Select all</button><button id="noRep">None</button></div>
  </div>
  <div class="panel"><h2>3 · Run</h2>
    <div class="row">
      <button id="startBtn" class="primary">Start</button>
      <button id="stopBtn" class="danger">Stop</button>
      <span id="jobState" class="badge legacy">idle</span>
      <span class="muted" id="jobDesc"></span>
    </div>
    <div class="bar"><div id="jobBar"></div></div>
    <div class="muted" id="jobNums"></div>
    <pre id="jobLog" style="margin-top:10px"></pre>
  </div>
</section>

<section id="tab-analytics" class="hide">
  <div class="row"><label class="muted">Session&nbsp;</label><select id="period"></select>
    <button id="reload">↻ Reload</button></div>
  <div class="cards" id="cards"></div>
  <div class="grid">
    <div class="panel"><h2>Revenue by center</h2><canvas id="revChart" height="230"></canvas></div>
    <div class="panel"><h2>New vs Repeat revenue</h2><canvas id="mixChart" height="230"></canvas></div>
  </div>
  <div class="panel"><h2>Per-center analysis <span class="muted" style="text-transform:none">(click a row for member detail)</span></h2>
    <div style="overflow:auto;max-height:430px"><table id="tbl"></table></div></div>
  <div class="grid">
    <div class="panel"><h2>Pack mix — revenue share</h2><canvas id="packChart" height="240"></canvas></div>
    <div class="panel"><h2>Pack breakdown</h2><div style="overflow:auto;max-height:300px"><table id="packTbl"></table></div></div>
  </div>
  <div class="panel hide" id="detailPanel"><h2 id="detailTitle"></h2>
    <div style="overflow:auto;max-height:430px"><table id="detail"></table></div></div>
</section>
</div>
<div id="toast"></div>

<script>
const TOKEN="__TOKEN__";
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const inr=n=>n==null?'-':'₹'+Number(n).toLocaleString('en-IN');
const num=n=>n==null?'-':Number(n).toLocaleString('en-IN');
const when=s=>s?String(s).replace('T',' ').slice(0,16):'';
async function api(path,opts){
  const r=await fetch(path,opts); const j=await r.json().catch(()=>({error:'Bad response'}));
  if(!r.ok) throw new Error(j.error||('HTTP '+r.status)); return j;
}
const post=(p,b)=>api(p,{method:'POST',headers:{'Content-Type':'application/json','X-Admin-Token':TOKEN},body:JSON.stringify(b||{})});
function toast(msg,bad){const t=$('toast');t.textContent=msg;t.style.borderColor=bad?'var(--bad)':'#333a48';
  t.style.display='block';clearTimeout(toast.h);toast.h=setTimeout(()=>t.style.display='none',5000);}
const guard=fn=>async(...a)=>{try{await fn(...a)}catch(e){toast(e.message,true)}};

/* ---------- tabs ---------- */
function showTab(name){
  for(const t of ['sessions','run','analytics']) $('tab-'+t).classList.toggle('hide',t!==name);
  document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.tab===name));
  if(name==='sessions') loadSessions(); if(name==='run') initRun(); if(name==='analytics') initPeriods();
}
$('tabs').addEventListener('click',e=>{if(e.target.dataset.tab)showTab(e.target.dataset.tab)});

/* ---------- login badge ---------- */
async function refreshLogin(fresh){
  const s=await api('/api/status'+(fresh?'?fresh=1':''));
  const b=$('loginBadge'); b.textContent=s.logged_in?'logged in':'not logged in';
  b.className='badge '+(s.logged_in?'complete':'incomplete');
  $('loginTxt').textContent=s.logged_in?'Session is valid — you can start a scrape.':'Not logged in. Send the OTP, then enter it below.';
  $('emailTxt').textContent=s.email; return s;
}

/* ---------- sessions ---------- */
let SESS=[];
async function loadSessions(){
  SESS=await api('/api/sessions');
  const h='<thead><tr><th>Session</th><th>Period</th><th class="l">Status</th><th class="l">Started</th><th>Centers</th><th>Missing</th><th class="l">Actions</th></tr></thead>';
  const rows=SESS.map((s,i)=>`<tr data-i="${i}"><td>${esc(s.session)}</td><td>${esc(s.start)} → ${esc(s.end)}</td>
    <td class="l"><span class="badge ${esc(s.status)}">${esc(s.status)}</span></td><td class="l">${esc(when(s.created_at))}</td>
    <td>${num(s.centers)}</td><td>${s.missing_count?num(s.missing_count):''}</td>
    <td class="l"><button data-act="details">Details</button>
      ${s.status==='incomplete'?'<button data-act="continue" class="primary">Continue</button>':''}
      ${s.folder?'<button data-act="merge">Merge</button>':''}</td></tr>`).join('');
  $('sessTbl').innerHTML=h+'<tbody>'+(rows||'<tr><td colspan="7" class="muted l">No sessions yet — start one in the Run tab.</td></tr>')+'</tbody>';
}
$('sessTbl').addEventListener('click',guard(async e=>{
  const b=e.target.closest('button'); if(!b) return; const s=SESS[b.closest('tr').dataset.i];
  if(b.dataset.act==='details') return showDetail(s);
  if(b.dataset.act==='continue'){ RUN_PRESET=s; return showTab('run'); }
  if(b.dataset.act==='merge'){ const r=await post('/api/merge',{session:s.session}); toast('Merged: '+r.files.length+' files'); }
}));
async function showDetail(s){
  const el=$('sessDetail'); el.classList.remove('hide');
  let html=`<h2>Session ${esc(s.session)} · ${esc(s.start)} → ${esc(s.end)}</h2>
    <div class="muted" style="margin-bottom:10px">${esc(s.folder||'Imported from before sessions existed — no folder')}</div>`;
  if(s.coverage){
    html+='<div style="overflow:auto"><table><thead><tr><th>Report</th><th>Requested</th><th>Already done</th><th>Scraped</th><th>Empty</th><th>Failed</th><th>Capped</th><th>Skipped</th></tr></thead><tbody>'+
      s.coverage.map(c=>`<tr><td>${esc(c.report)}</td><td>${num(c.requested)}</td><td>${num(c.already_done)}</td><td>${num(c.scraped)}</td>
      <td style="color:${c.empty?'var(--warn)':''}">${num(c.empty)}</td><td style="color:${c.failed?'var(--bad)':''}">${num(c.failed)}</td>
      <td>${num(c.possibly_truncated)}</td><td>${c.skipped_after_failures?'yes':''}</td></tr>`).join('')+'</tbody></table></div>';
  } else html+='<div class="muted">No coverage recorded for this session.</div>';
  const miss=Object.entries(s.missing||{});
  if(miss.length) html+='<h2 style="margin-top:16px">Still missing</h2>'+miss.map(([r,c])=>`<div><b>${esc(r)}</b> <span class="muted">${esc(c.join(', '))}</span></div>`).join('');
  el.innerHTML=html;
  if(s.folder){
    const f=await api('/api/session_files?session='+encodeURIComponent(s.session));
    el.innerHTML+='<h2 style="margin-top:16px">Files <span class="muted" style="text-transform:none">('+f.center_folders+' center folders)</span></h2>'+
      '<div class="checks">'+f.files.map(x=>`<div><a style="color:var(--accent)" href="/download?session=${encodeURIComponent(s.session)}&path=${encodeURIComponent(x.path)}">${esc(x.path)}</a> <span class="muted">${(x.size/1024).toFixed(1)} KB</span></div>`).join('')+'</div>';
  }
  el.scrollIntoView({behavior:'smooth'});
}

/* ---------- run ---------- */
let CFG=null, RUN_PRESET=null, POLL=null;
async function initRun(){
  if(!CFG){
    CFG=await api('/api/config');
    $('start').value=CFG.default_start; $('end').value=CFG.default_end;
    $('reports').innerHTML=CFG.reports.map(r=>`<label><input type="checkbox" value="${esc(r)}" checked> ${esc(r)}</label>`).join('');
    $('centerCount').textContent=CFG.centers+' centers in centers_to_process.csv';
  }
  if(RUN_PRESET){ $('start').value=RUN_PRESET.start; $('end').value=RUN_PRESET.end; }
  await refreshLogin(false); await checkResume(); pollJob(); if(!POLL) POLL=setInterval(pollJob,2000);
}
async function checkResume(){
  const row=$('resumeRow'); const st=$('start').value, en=$('end').value;
  let sid=RUN_PRESET?RUN_PRESET.session:null, label='';
  if(!sid && st && en){ const u=await api(`/api/unfinished?start=${st}&end=${en}`); sid=u.session; label=u.created_at; }
  else if(sid) label=RUN_PRESET.created_at;
  if(!sid){ row.innerHTML='<span class="muted">No unfinished session for this period — a new session will start.</span>'; return; }
  row.innerHTML=`<label><input type="radio" name="mode" value="continue" checked> Continue session <b>${esc(sid)}</b> <span class="muted">(started ${esc(when(label))}; only missing center/sections are fetched)</span></label>
    <label><input type="radio" name="mode" value="new"> Start a new session</label>`;
  row.dataset.sid=sid;
}
['start','end'].forEach(id=>$(id).addEventListener('change',()=>{RUN_PRESET=null;guard(checkResume)()}));
$('allRep').onclick=()=>document.querySelectorAll('#reports input').forEach(i=>i.checked=true);
$('noRep').onclick=()=>document.querySelectorAll('#reports input').forEach(i=>i.checked=false);
$('sendOtp').onclick=guard(async()=>{const r=await post('/api/login/send'); toast(r.ok?'OTP sent — check the inbox':'Could not send the OTP',!r.ok);});
$('verifyOtp').onclick=guard(async()=>{const r=await post('/api/login/verify',{otp:$('otp').value});
  $('otp').value=''; toast(r.ok?'Logged in':'OTP not accepted',!r.ok); await refreshLogin(true);});
$('startBtn').onclick=guard(async()=>{
  const picked=[...document.querySelectorAll('#reports input:checked')].map(i=>i.value);
  if(!picked.length) throw new Error('Select at least one report');
  const mode=document.querySelector('input[name=mode]:checked');
  const body={start:$('start').value,end:$('end').value,only:picked.length===CFG.reports.length?null:picked};
  if(mode&&mode.value==='continue') body.session=$('resumeRow').dataset.sid;
  await post('/api/run',body); RUN_PRESET=null; toast('Started'); pollJob();
});
$('stopBtn').onclick=guard(async()=>{await post('/api/stop'); toast('Stopping after the current request…');});
async function pollJob(){
  if($('tab-run').classList.contains('hide')) return;
  const j=await api('/api/job'); const pct=j.total?Math.round(100*j.done/j.total):0;
  $('jobBar').style.width=pct+'%'; $('jobState').textContent=j.state; $('jobState').className='badge '+(j.state==='running'?'running':j.state==='error'?'incomplete':j.state==='done'?'complete':'legacy');
  $('jobDesc').textContent=j.error||j.desc||(j.state==='done'?('session '+j.session+' '+(j.session_status||'')):'');
  $('jobNums').textContent=j.total?`${j.done} / ${j.total} (${pct}%)${j.session?' · session '+j.session:''}`:'';
  $('jobLog').textContent=j.log.join('\n'); $('jobLog').scrollTop=1e9;
  $('startBtn').disabled=j.state==='running'; $('stopBtn').disabled=j.state!=='running';
}

/* ---------- analytics ---------- */
let revChart,mixChart,packChart,CURRENT;
async function initPeriods(){
  const ps=await api('/api/periods'); const sel=$('period');
  if(!ps.length){sel.innerHTML='<option>no data yet</option>';return;}
  sel.innerHTML=ps.map(p=>`<option value="${esc(p.session)}|${esc(p.start)}|${esc(p.end)}">${esc(p.start)} → ${esc(p.end)} · session ${esc(p.session)} (${p.centers} centers)</option>`).join('');
  load();
}
$('reload').onclick=guard(load); $('period').onchange=guard(load);
async function load(){
  const v=$('period').value; if(!v||v==='no data yet')return;
  const [session,start,end]=v.split('|'); CURRENT={session,start,end};
  const d=await api(`/api/analysis?session=${encodeURIComponent(session)}&start=${start}&end=${end}`);
  renderCards(d.totals); renderTable(d); renderCharts(d); renderPacks(d.packs); $('detailPanel').classList.add('hide');
}
function renderCards(t){
  const c=[['Centers',num(t.centers)],['Sales count',num(t.sales_count)],['Sales revenue',inr(t.sales_revenue)],
    ['Total revenue',inr(t.total_revenue)],['New revenue',inr(t.new_revenue)],['Repeat revenue',inr(t.repeat_revenue)],
    ['Avg ticket',inr(t.avg_ticket)],['Active rate',t.active_rate+'%']];
  $('cards').innerHTML=c.map(x=>`<div class="card"><div class="lbl">${esc(x[0])}</div><div class="val">${esc(x[1])}</div></div>`).join('');
}
function renderTable(d){
  const H=d.header,R=d.rows,ex=d.extra||{},money=[3,6,7,8];
  let t='<thead><tr>'+H.concat(['active_%','avg_ticket']).map(h=>`<th>${esc(h)}</th>`).join('')+'</tr></thead><tbody>';
  t+=R.map(r=>{const e=ex[String(r[0])]||{};
    return `<tr class="click" data-cid="${esc(r[0])}" data-cname="${esc(r[1])}">`+r.map((v,i)=>`<td>${v==null?'':esc(money.includes(i)?inr(v):v)}</td>`).join('')+
      `<td>${e.active_rate==null?'':esc(e.active_rate)+'%'}</td><td>${esc(inr(e.avg_ticket))}</td></tr>`}).join('');
  $('tbl').innerHTML=t+'</tbody>';
}
$('tbl').addEventListener('click',guard(async e=>{
  const tr=e.target.closest('tr.click'); if(!tr) return;
  document.querySelectorAll('#tbl tr').forEach(x=>x.classList.remove('sel')); tr.classList.add('sel');
  const d=await api(`/api/sales?session=${encodeURIComponent(CURRENT.session)}&start=${CURRENT.start}&end=${CURRENT.end}&center_id=${encodeURIComponent(tr.dataset.cid)}`);
  let t='<thead><tr>'+d.header.map(h=>`<th>${esc(h)}</th>`).join('')+'</tr></thead><tbody>';
  t+=d.rows.map(r=>'<tr>'+r.map((v,i)=>`<td>${v==null?'':esc(i===7?inr(v):v)}</td>`).join('')+'</tr>').join('');
  $('detail').innerHTML=t+'</tbody>'; $('detailTitle').textContent=`Member detail — ${tr.dataset.cname} (${d.rows.length} sales)`;
  $('detailPanel').classList.remove('hide');
}));
function renderPacks(packs){
  packs=packs||[];
  $('packTbl').innerHTML='<thead><tr><th>Pack</th><th>Sales</th><th>Revenue</th></tr></thead><tbody>'+
    packs.map(p=>`<tr><td>${esc(p.pack)}</td><td>${num(p.count)}</td><td>${esc(inr(p.revenue))}</td></tr>`).join('')+'</tbody>';
  const top=packs.filter(p=>p.revenue>0).slice(0,8), pal=['#4f8cff','#33c48d','#f5a623','#e5567b','#9b6dff','#2ec5c5','#c9d14a','#ff8a5c'];
  packChart&&packChart.destroy();
  packChart=new Chart($('packChart'),{type:'doughnut',data:{labels:top.map(p=>p.pack),datasets:[{data:top.map(p=>p.revenue),backgroundColor:pal}]},
    options:{plugins:{legend:{position:'right',labels:{color:'#c7cdd8',boxWidth:12,font:{size:11}}}}}});
}
function renderCharts(d){
  const H=d.header,R=d.rows,idx=n=>H.indexOf(n),names=R.map(r=>r[1]||r[0]);
  const opt=st=>({plugins:{legend:{labels:{color:'#c7cdd8'}}},scales:{x:{stacked:st,ticks:{color:'#8b93a1'}},y:{stacked:st,ticks:{color:'#8b93a1'}}}});
  revChart&&revChart.destroy(); mixChart&&mixChart.destroy();
  revChart=new Chart($('revChart'),{type:'bar',data:{labels:names,datasets:[{label:'Total revenue',data:R.map(r=>r[idx('total_revenue')]),backgroundColor:'#4f8cff'}]},
    options:{...opt(false),plugins:{legend:{display:false}}}});
  mixChart=new Chart($('mixChart'),{type:'bar',data:{labels:names,datasets:[{label:'New',data:R.map(r=>r[idx('new_revenue')]),backgroundColor:'#33c48d'},
    {label:'Repeat',data:R.map(r=>r[idx('repeat_revenue')]),backgroundColor:'#f5a623'}]},options:opt(true)});
}

guard(async()=>{await refreshLogin(false); await loadSessions();})();
setInterval(()=>guard(()=>refreshLogin(false))(),30000);
</script></body></html>"""


def main():
    global DB_PATH, PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    DB_PATH = args.db if os.path.isabs(args.db) else os.path.join(HERE, args.db)
    PORT = args.port
    C.QUIET = True  # login helpers must not print tokens or cookies

    # databases from before sessions need the session columns (rows become 'legacy')
    conn = sqlite3.connect(DB_PATH)
    S.init_db(conn)
    conn.close()

    url = f"http://localhost:{PORT}/"
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Curefit admin: {url}   (DB: {DB_PATH})")
    print("Press Ctrl+C to stop.")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
