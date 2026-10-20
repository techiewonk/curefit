"""Logic behind the console pages (kept free of HTTP so it is easy to test)."""
import datetime as dt
import re
import statistics
import time

from constance import config
from django.db.models import Count, Q, Sum

import curefit_login as C
from centers import services as center_services
from centers import sync
from centers.models import Center, CenterList, CenterSyncLog, KnownEmpty
from scraper import analysis, conf, engine, jobs
from scraper.models import SalesRecord, SectionResult, Session
from scraper.reports import GROUPS, REPORTS

_auth = {"at": 0.0, "ok": False}
PAIR_OVERHEAD = 2.0  # average polite delay between requests, in seconds
DEFAULT_SECONDS = 40.0


class ConsoleError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ login
def logged_in(fresh=False):
    if fresh or time.time() - _auth["at"] > 30:
        _auth.update(at=time.time(), ok=C.session_ok())
    return _auth["ok"]


def send_otp():
    r = C.send_otp(config.LOGIN_EMAIL)
    return {"ok": r.status_code == 200, "email": config.LOGIN_EMAIL}


def verify_otp(otp):
    otp = str(otp or "").strip()
    if not re.fullmatch(r"\d{4,8}", otp):
        raise ConsoleError("Enter the numeric OTP from the email")
    r = C.validate_otp(otp, config.LOGIN_EMAIL)
    try:
        ok = r.status_code == 200 and "token" in r.json()
    except ValueError:
        ok = False
    if ok:
        sr = C.establish_session()
        ok = sr is not None and sr.status_code == 200
    _auth.update(at=time.time(), ok=bool(ok) and C.session_ok())
    return {"ok": _auth["ok"]}


# ------------------------------------------------------------------ run page
def run_config():
    today = dt.date.today()
    last_prev = today.replace(day=1) - dt.timedelta(days=1)
    return {
        "email": config.LOGIN_EMAIL,
        "reports": [{"name": n, "label": r["label"]} for n, r in REPORTS.items()],
        "groups": GROUPS,
        "speeds": conf.SPEED_PROFILES,
        "lists": {lst.name: lst.centers.count() for lst in CenterList.objects.all()},
        "active_centers": Center.objects.filter(state="ACTIVE").count(),
        "default_start": last_prev.replace(day=1).isoformat(),
        "default_end": last_prev.isoformat(),
        "today": today.isoformat(),
    }


def timings():
    """Seconds per section measured in earlier sessions (median and mean per report)."""
    per = {}
    for report, seconds in SectionResult.objects.filter(
            status=SectionResult.OK, seconds__isnull=False).values_list("report", "seconds"):
        per.setdefault(report, []).append(seconds)
    return {r: {"median": statistics.median(v), "mean": statistics.mean(v), "n": len(v)}
            for r, v in per.items() if len(v) >= 3}


def estimate(list_name, only, workers):
    if list_name not in CenterList.objects.values_list("name", flat=True):
        raise ConsoleError("Unknown center list")
    reports = only or list(REPORTS)
    if not all(r in REPORTS for r in reports):
        raise ConsoleError("Unknown report in selection")
    n = CenterList.objects.get(name=list_name).centers.count()
    workers = max(1, min(int(workers or 1), config.MAX_WORKERS))
    t = timings()
    low = sum(t[r]["median"] + PAIR_OVERHEAD if r in t else DEFAULT_SECONDS for r in reports) * n / workers
    high = sum(t[r]["mean"] + PAIR_OVERHEAD if r in t else DEFAULT_SECONDS * 1.4 for r in reports) * n / workers
    return {"centers": n, "reports": len(reports), "pairs": n * len(reports), "workers": workers,
            "seconds_low": round(low), "seconds_high": round(high),
            "measured": sorted(r for r in reports if r in t),
            "assumed": sorted(r for r in reports if r not in t)}


def unfinished(start, end):
    s = engine.find_unfinished(start, end)
    return {"session": s.session_id if s else None,
            "created_at": s.created_at.isoformat() if s else None,
            "missing": s.missing_count if s else 0}


def start_run(body):
    only = body.get("only") or None
    spec = {"start": body.get("start"), "end": body.get("end"), "list": body.get("list") or "actual",
            "only": only, "workers": body.get("workers"), "session": body.get("session"),
            "retry_empty": bool(body.get("retry_empty")), "speed": body.get("speed", "normal")}
    for key in ("start", "end"):
        if spec[key]:
            try:
                dt.date.fromisoformat(spec[key])
            except ValueError:
                raise ConsoleError(f"Not a valid date: {spec[key]}")
    if not logged_in(fresh=True):
        raise ConsoleError("Not logged in - send and verify the OTP first", 401)
    try:
        return {"ok": True, "job": jobs.enqueue(spec).id}
    except jobs.JobError as e:
        raise ConsoleError(str(e), e.code)


def stop_run():
    job = jobs.active_job()
    if job:
        jobs.request_stop(job)
    return {"ok": True}


def pause_run(on):
    job = jobs.active_job()
    if job is None:
        raise ConsoleError("No scrape is running", 409)
    jobs.set_pause(job, on)
    return {"ok": True, "paused": bool(on)}


def status():
    return {"logged_in": logged_in(), "email": config.LOGIN_EMAIL, "job": jobs.payload(jobs.latest_job())}


# ------------------------------------------------------------------ monitor
GLYPH = {SectionResult.OK: "o", SectionResult.EMPTY: "e", SectionResult.FAILED: "f",
         SectionResult.SLOW: "s", SectionResult.SKIPPED: "k"}


def monitor(session_id):
    """Centers x reports grid. o ok  e empty  f failed  s slow  k skipped  r running  . waiting"""
    try:
        s = Session.objects.get(pk=session_id)
    except Session.DoesNotExist:
        raise ConsoleError(f"Unknown session {session_id}", 404)
    ids = [int(c) for c in s.center_ids]
    reports = list(s.reports)
    names = dict(Center.objects.filter(center_id__in=ids).values_list("center_id", "name"))
    seen = {(c, r): (st, n) for c, r, st, n in
            SectionResult.objects.filter(session=s).values_list("center_id", "report", "status", "row_count")}
    errors = {f"{c}|{r}": e[:200] for c, r, e in SectionResult.objects.filter(
        session=s, status__in=[SectionResult.FAILED, SectionResult.SLOW]).values_list("center_id", "report", "error")}
    job = jobs.latest_job()
    live = bool(job and job.session_id == s.pk and job.state in jobs.ACTIVE)
    active = set(job.active) if live else set()
    grid, rows = [], []
    for cid in ids:
        line, nums = [], []
        for rep in reports:
            hit = seen.get((cid, rep))
            if hit:
                line.append(GLYPH.get(hit[0], "?"))
                nums.append(hit[1])
            elif f"{cid}|{rep}" in active:
                line.append("r")
                nums.append(-1)
            else:
                line.append(".")
                nums.append(-1)
        grid.append("".join(line))
        rows.append(nums)
    return {"session": s.session_id, "status": s.status, "start": str(s.start_date), "end": str(s.end_date),
            "reports": reports, "labels": {r: REPORTS[r]["label"] for r in reports if r in REPORTS},
            "centers": [[c, names.get(c, "")] for c in ids], "grid": grid, "rows": rows, "errors": errors, "live": live,
            "job": jobs.payload(job) if live else None}


def session_choices():
    return [{"session": s.session_id, "start": str(s.start_date), "end": str(s.end_date),
             "status": s.status, "created_at": s.created_at.isoformat(), "centers": len(s.center_ids or [])}
            for s in Session.objects.all()[:60]]


# ------------------------------------------------------------------ analytics
def analytics(session_id):
    try:
        s = Session.objects.get(pk=session_id)
    except Session.DoesNotExist:
        raise ConsoleError(f"Unknown session {session_id}", 404)
    result = analysis.build_analysis(s)
    rows = result["rows"]
    extra = {}
    for r in (SalesRecord.objects.filter(session=s).values("center_id")
              .annotate(n=Count("id"), act=Count("id", filter=Q(active_last_30d="Yes")), rev=Sum("revenue"))):
        extra[str(r["center_id"])] = {
            "active_count": r["act"], "active_rate": round(100.0 * r["act"] / r["n"], 1) if r["n"] else 0,
            "avg_ticket": round((r["rev"] or 0) / r["n"], 0) if r["n"] else 0}
    packs = [{"pack": p["pack_name"], "count": p["n"], "revenue": round(p["rev"] or 0, 0)}
             for p in SalesRecord.objects.filter(session=s).values("pack_name")
             .annotate(n=Count("id"), rev=Sum("revenue")).order_by("-n")]
    sc, sr = sum((r[2] or 0) for r in rows), sum((r[3] or 0) for r in rows)
    ac = sum(e["active_count"] for e in extra.values())
    totals = {"centers": len(rows), "sales_count": sc, "sales_revenue": sr,
              "total_revenue": sum((r[6] or 0) for r in rows), "new_revenue": sum((r[7] or 0) for r in rows),
              "repeat_revenue": sum((r[8] or 0) for r in rows),
              "avg_ticket": round(sr / sc, 0) if sc else 0, "active_rate": round(100.0 * ac / sc, 1) if sc else 0}
    return {"header": result["header"], "rows": rows, "extra": extra, "packs": packs, "totals": totals,
            "start": str(s.start_date), "end": str(s.end_date)}


def sales_detail(session_id, center_id):
    qs = SalesRecord.objects.filter(session_id=session_id, center_id=int(center_id)) \
        .order_by("-revenue", "name")
    cols = ["user_id", "name", "phone", "pack_name", "member_end_date", "active_last_30d", "renewed", "revenue"]
    return {"header": cols, "rows": list(qs.values_list(*cols))}


def compare(a_id, b_id):
    """Per-center sales/revenue in two sessions and the difference (b minus a)."""
    out = {}
    for tag, sid in (("a", a_id), ("b", b_id)):
        try:
            s = Session.objects.get(pk=sid)
        except Session.DoesNotExist:
            raise ConsoleError(f"Unknown session {sid}", 404)
        for r in analysis.build_analysis(s)["rows"]:
            out.setdefault(r[0], {"name": r[1]})[tag] = {"sales": r[2], "sales_revenue": r[3], "total_revenue": r[6]}
    rows = []
    for cid in sorted(out):
        a, b = out[cid].get("a"), out[cid].get("b")
        d = None
        if a and b:
            d = {k: (b[k] or 0) - (a[k] or 0) for k in a}
        rows.append({"center_id": cid, "name": out[cid]["name"], "a": a, "b": b, "diff": d})
    return {"a": a_id, "b": b_id, "rows": rows}


# ------------------------------------------------------------------ centers
def centers_payload():
    return {"centers": [{"id": c.center_id, "name": c.name, "code": c.code, "state": c.state,
                         "type": c.center_type, "cluster": c.cluster}
                        for c in Center.objects.all()],
            "lists": {lst.name: sorted(lst.centers.values_list("center_id", flat=True))
                      for lst in CenterList.objects.all()},
            "synced_at": (lambda l: l.synced_at.isoformat() if l else None)(CenterSyncLog.objects.first())}


def sync_centers():
    if not logged_in(fresh=True):
        raise ConsoleError("Not logged in - send and verify the OTP on the Run page first", 401)
    try:
        return sync.sync_from_kronos()
    except sync.NotLoggedIn as e:
        raise ConsoleError(str(e), 401)
    except Exception as e:
        raise ConsoleError(f"Sync failed: {e}", 502)


def save_list(name, ids):
    if not re.fullmatch(r"[-a-z0-9_]{1,40}", name or ""):
        raise ConsoleError("Bad list name")
    try:
        return center_services.set_list(name, ids)
    except ValueError as e:
        raise ConsoleError(str(e))


# ------------------------------------------------------------------ home
def home():
    sessions = list(Session.objects.exclude(status=Session.LEGACY)[:6]) or list(Session.objects.all()[:6])
    job = jobs.latest_job()
    alerts = []
    if job and job.state in (job.ERROR, job.INTERRUPTED):
        alerts.append({"level": "bad", "text": job.error or f"The last job ended as {job.state}.", "go": "console:run"})
    if not logged_in():
        alerts.append({"level": "warn", "text": "Not logged in to Kronos - a scrape needs a fresh OTP.", "go": "console:run"})
    open_sessions = Session.objects.filter(status__in=[Session.INCOMPLETE, Session.INTERRUPTED])
    if open_sessions.exists():
        u = open_sessions.first()
        alerts.append({"level": "warn", "go": "console:run",
                       "text": f"{open_sessions.count()} unfinished session(s); latest {u.session_id} "
                               f"({u.start_date} to {u.end_date}) is missing {u.missing_count} center/section pairs."})
    done = Session.objects.exclude(coverage__isnull=True).first()
    if done:
        cov = done.coverage
        failed, slow = sum(x["failed"] for x in cov), sum(x.get("slow", 0) for x in cov)
        empty, capped = sum(x["empty"] for x in cov), sum(x["possibly_truncated"] for x in cov)
        skipped = [x["report"] for x in cov if x["skipped_after_failures"]]
        if failed:
            alerts.append({"level": "bad", "text": f"Session {done.session_id}: {failed} failed center/section pairs."})
        if skipped:
            alerts.append({"level": "bad", "text": f"Reports skipped after repeated failures: {', '.join(skipped)}."})
        if slow:
            alerts.append({"level": "warn", "text": f"Session {done.session_id}: {slow} slow (timed-out) sections."})
        if capped:
            alerts.append({"level": "warn", "text": f"{capped} result(s) hit the row cap and may be incomplete."})
        if empty:
            known = KnownEmpty.objects.count()
            alerts.append({"level": "info", "text": f"Session {done.session_id}: {empty} empty center/section pairs"
                                                    f" ({known} known-empty registered)."})
    latest = Session.objects.filter(status=Session.COMPLETE).first() or Session.objects.first()
    kpis = analytics(latest.pk)["totals"] if latest and SalesRecord.objects.filter(session=latest).exists() else None
    return {"logged_in": logged_in(), "email": config.LOGIN_EMAIL, "job": jobs.payload(job), "alerts": alerts,
            "sessions": sessions, "latest": latest, "kpis": kpis,
            "lists": {lst.name: lst.centers.count() for lst in CenterList.objects.all()},
            "active_centers": Center.objects.filter(state="ACTIVE").count()}
