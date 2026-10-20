"""
Logic behind the web admin (no web framework in here, so it is easy to test).

  data      sessions, coverage, analytics, centers, downloads
  login     send / verify the emailed OTP
  jobs      scrapes run in a separate worker process (worker.py); this module only writes the
            job spec, starts the worker, reads its status file and drops a stop file.
            Because the worker is its own process, restarting or auto-reloading the web server
            never interrupts a running scrape.
"""
import datetime as dt
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import uuid

import centers_sync as CS
import curefit_login as C
import scrape_sales as S

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CUREFIT_DB") or os.path.join(HERE, "curefit.db")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_WORKERS = 4
STALE_AFTER = 45  # seconds without a heartbeat before a "running" job is declared dead

_auth = {"at": 0.0, "ok": False}


class ApiError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def jobs_dir():
    d = os.path.join(S.RUNS_DIR, ".jobs")
    os.makedirs(d, exist_ok=True)
    return d


def admin_token():
    """Stable per-install secret, so an open page keeps working across server reloads."""
    path = os.path.join(S.RUNS_DIR, ".admin_secret")
    if not os.path.exists(path):
        os.makedirs(S.RUNS_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(32))
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = db()
    S.init_db(conn)
    conn.close()


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


def session_info(sid):
    info = S.find_session(sid)
    if not info:
        raise ApiError(f"Unknown session '{sid}'", 404)
    return info


def session_files(sid):
    info = session_info(sid)
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


def download_path(sid, rel):
    """Absolute path of a file inside a session folder (never outside it)."""
    root = os.path.realpath(session_info(sid)["folder"])
    full = os.path.realpath(os.path.join(root, rel))
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        raise ApiError("File not found", 404)
    return full


def merge_session(sid):
    info = session_info(sid)
    return [os.path.basename(f) for f in S.merge_session(info["folder"])]


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


# ------------------------------ centers ------------------------------
def centers_payload():
    return {"centers": CS.load_centers(), "synced_at": CS.synced_at(),
            "lists": {k: CS.read_list(k) for k in CS.LISTS}}


def sync_centers_api():
    if not logged_in(fresh=True):
        raise ApiError("Not logged in — send and verify the OTP in the Run tab first", 401)
    try:
        return CS.sync_centers()
    except PermissionError as e:
        raise ApiError(str(e), 401)
    except Exception as e:
        raise ApiError(f"Sync failed: {e}", 502)


def save_list_api(name, ids):
    if name not in CS.LISTS:
        raise ApiError("Unknown center list")
    if job_payload()["state"] in ("running", "starting"):
        raise ApiError("A scrape is running — wait for it to finish before changing the lists", 409)
    try:
        return CS.write_list(name, ids)
    except ValueError as e:
        raise ApiError(str(e))


def config_payload():
    today = dt.date.today()
    last_prev = today.replace(day=1) - dt.timedelta(days=1)
    return {"reports": list(S.REPORTS), "email": C.EMAIL, "max_workers": MAX_WORKERS,
            "lists": {k: len(CS.read_list(k)) for k in CS.LISTS},
            "active_centers": sum(1 for c in CS.load_centers() if c["state"] == "ACTIVE"),
            "default_start": last_prev.replace(day=1).isoformat(),
            "default_end": last_prev.isoformat()}


# ------------------------------ login ------------------------------
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


# ------------------------------ jobs (separate worker process) ------------------------------
def write_json_atomic(path, obj):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    for _ in range(10):  # a reader may hold the file open for a moment on Windows
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def read_json(path):
    for _ in range(5):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            time.sleep(0.05)
    return None


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _latest_job_id():
    files = [f for f in os.listdir(jobs_dir()) if f.endswith(".spec.json")]
    if not files:
        return None
    files.sort(key=lambda f: os.path.getmtime(os.path.join(jobs_dir(), f)))
    return files[-1][: -len(".spec.json")]


def job_payload(jid=None):
    """Status of the newest job (or a given one), including a check that its worker is alive."""
    jid = jid or _latest_job_id()
    if not jid:
        return {"state": "idle", "log": [], "done": 0, "total": 0, "desc": "", "error": None,
                "session": None}
    spec_path = os.path.join(jobs_dir(), f"{jid}.spec.json")
    st = read_json(os.path.join(jobs_dir(), f"{jid}.json"))
    if st is None:  # worker has not reported yet
        age = time.time() - os.path.getmtime(spec_path)
        st = {"id": jid, "state": "starting" if age < 60 else "error",
              "error": None if age < 60 else "Worker did not start", "log": [], "done": 0,
              "total": 0, "desc": "starting…", "session": None, "updated_at": time.time()}
        return st
    if st.get("state") in ("running", "starting"):
        beat = time.time() - st.get("updated_at", 0)
        if beat > STALE_AFTER and not pid_alive(st.get("pid")):
            st.update(state="error", error="The worker process stopped unexpectedly "
                                           "(continue the session to finish it)")
    st["log"] = st.get("log", [])[-60:]
    st["id"] = jid
    return st


def _spawn(jid):
    """Start worker.py as its own process, detached from this server."""
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    log = open(os.path.join(jobs_dir(), f"{jid}.log"), "ab")
    subprocess.Popen([sys.executable, "-X", "utf8", os.path.join(HERE, "worker.py"), jid],
                     cwd=HERE, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kw)


def start_job(body):
    start, end = body.get("start", ""), body.get("end", "")
    session = None
    if body.get("session"):
        session = session_info(body["session"])
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
    list_name = body.get("list") or "actual"
    if list_name not in CS.LISTS:
        raise ApiError("Unknown center list")
    if session is None and not CS.read_list(list_name):
        raise ApiError(f"The {list_name} center list is empty — pick centers in the Centers tab")
    only = body.get("only") or None
    if only is not None and (not isinstance(only, list) or not all(o in S.REPORTS for o in only)):
        raise ApiError("Unknown report in selection")
    try:
        workers = int(body.get("workers") or 1)
    except (TypeError, ValueError):
        raise ApiError("workers must be a number")
    if not 1 <= workers <= MAX_WORKERS:
        raise ApiError(f"workers must be between 1 and {MAX_WORKERS}")
    if not logged_in(fresh=True):
        raise ApiError("Not logged in — send and verify the OTP first", 401)
    if job_payload()["state"] in ("running", "starting"):
        raise ApiError("A scrape is already running", 409)

    jid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    spec = {"id": jid, "start": start, "end": end, "session": body.get("session"), "only": only,
            "centers_csv": CS.LISTS[list_name], "db": DB_PATH, "workers": workers}
    write_json_atomic(os.path.join(jobs_dir(), f"{jid}.spec.json"), spec)
    _spawn(jid)
    return {"ok": True, "job": jid}


def stop_job():
    st = job_payload()
    if st["state"] not in ("running", "starting"):
        return {"ok": True, "state": st["state"]}
    with open(os.path.join(jobs_dir(), f"{st['id']}.stop"), "w") as f:
        f.write("stop")
    return {"ok": True, "state": "stopping"}
