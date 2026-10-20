"""
The scrape itself: for every center and report of a session, fetch, store, write files.

Behaviour carried over from the script version: one signed token per center+dashboard, an empty
result gets one retry on a fresh token, a report that keeps failing is skipped, repeated 401/403
stops the run, everything already saved is skipped on resume, centers can run side by side.

Progress is reported through an optional `status` dict (the job runner mirrors it into the Job
row): set status["stop"] / status["pause"] to stop or hold the run cleanly.
"""
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests
from constance import config
from django.db import connection
from django.utils import timezone

from centers.models import Center, KnownEmpty

from . import checks, conf, coverage, customers, files, kronos, login, masters
from .throttle import Gate
from .models import SectionResult, Session
from .reports import REPORTS
from .storage import record_result, save_section

log = logging.getLogger("scraper.engine")
DONE_STATES = (SectionResult.OK, SectionResult.EMPTY)  # failed/slow sections are retried on resume


# ------------------------------------------------------------------ sessions
def new_session(start, end, center_ids, reports, speed="normal", workers=1, granularity=Session.MONTH,
                sales_chunk=Session.WHOLE, fill_missing=False):
    sid = uuid.uuid4().hex[:6]
    session = Session.objects.create(
        session_id=sid, start_date=start, end_date=end, status=Session.RUNNING,
        folder=files.session_folder(start, end, sid), reports=list(reports),
        center_ids=[int(c) for c in center_ids], speed=speed, workers=workers, granularity=granularity,
        sales_chunk=sales_chunk, fill_missing=bool(fill_missing))
    files.write_session_json(session)
    return session


def find_unfinished(start, end, granularity=None):
    """Latest session for exactly this period (and split) that did not complete."""
    return Session.objects.for_period(start, end, granularity).unfinished().first()


# ------------------------------------------------------------------ run
def run_session(session, workers=1, status=None, refresh=False, only=None, adaptive=False,
                recheck_known=False):
    """Fetch everything the session has not saved yet. Returns the updated Session.

    adaptive: start with `workers` centers at a time and let the run slow down or speed up by itself
    (up to the MAX_WORKERS setting). recheck_known: also ask for the known-empty center/reports.
    """
    start, end = str(session.start_date), str(session.end_date)
    ids = [int(c) for c in session.center_ids]
    reports = [r for r in (only or session.reports) if r in REPORTS]
    cfg = conf.RunSettings.load()
    workers = max(1, min(int(workers or 1), cfg.max_workers))
    gate = Gate(workers, cfg.max_workers if adaptive else workers, adaptive)
    known_empty = set() if recheck_known else set(KnownEmpty.objects.values_list("center_id", "report"))
    centers = Center.objects.in_bulk(ids)

    session.status = Session.RUNNING
    session.finished_at = None
    session.save(update_fields=["status", "finished_at"])
    files.write_session_json(session)

    http, _ = login.load_session()
    stats, done = {}, {}
    for rep in reports:
        done[rep] = set() if refresh else set(SectionResult.objects.filter(
            session=session, report=rep, status__in=DONE_STATES).values_list("center_id", flat=True))
        stats[rep] = {"requested": len(ids), "already_done": len(done[rep] & set(ids)), "ok": 0,
                      "empty": [], "known_empty": [], "failed": [], "slow": [], "capped": [], "skipped": False}

    groups = {}  # reports of one dashboard share a signed token per center
    for rep in reports:
        groups.setdefault(REPORTS[rep]["dashboard"], []).append(rep)

    total = sum(len([c for c in ids if c not in done[r]]) for r in reports)
    if status is not None:
        status.update(state="running", session=session.session_id, folder=session.folder, done=0,
                      total=total, desc="", error=None, stop=False, pause=False, log=[], cells={},
                      active=[], started_at=time.time(), workers=workers)

    lock = threading.Lock()
    flags = {"auth_failures": 0, "aborted": False, "stopped": False}
    fail_streak = {r: 0 for r in reports}
    row_cap, auth_limit, report_limit = cfg.row_cap, cfg.auth_fail_limit, cfg.report_fail_limit

    def note(msg):
        if status is not None:
            with lock:
                status["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")
                del status["log"][:-200]

    def advance():
        if status is not None:
            with lock:
                status["done"] += 1

    def mark(cid, rep, state):
        """Live cell state for the monitor: active while in flight, 'failed'/'slow' afterwards."""
        if status is None:
            return
        with lock:
            key = f"{cid}|{rep}"
            if state == "active":
                status["active"].append(key)
                return
            if key in status["active"]:
                status["active"].remove(key)
            if state in ("failed", "slow"):
                status["cells"][key] = state

    def process_center(cid):
        center = centers.get(cid) or Center.objects.get_or_create(center_id=cid)[0]
        label = center.name or center.code
        tmb = requests.Session()
        tmb.cookies.set("metabase.DEVICE", str(uuid.uuid4()))
        try:
            for reps in groups.values():
                token = None
                for rep_name in [r for r in reps if cid not in done[r]]:
                    if flags["aborted"]:
                        return
                    while status is not None and status.get("pause") and not status.get("stop"):
                        time.sleep(1)  # paused from the admin: hold until resumed or stopped
                    if status is not None and status.get("stop"):
                        with lock:
                            flags["aborted"] = flags["stopped"] = True
                        return
                    st = stats[rep_name]
                    if st["skipped"]:
                        advance()
                        continue
                    if (cid, rep_name) in known_empty:  # always empty: do not spend minutes asking again
                        record_result(session, cid, rep_name, SectionResult.EMPTY, 0, 0, "known empty: not asked")
                        with lock:
                            st["known_empty"].append(cid)
                        note(f"known-empty {rep_name} · {cid} {label}")
                        advance()
                        continue
                    rep = REPORTS[rep_name]
                    if status is not None:
                        status["desc"] = f"{rep_name} · center {cid} {label}"
                    mark(cid, rep_name, "active")
                    t0 = time.monotonic()
                    try:
                        rows, cols, fetched = [], [], None
                        if rep_name == "sales" and session.sales_chunk != Session.WHOLE:
                            # a day or a week at a time; a day with no expiries is simply empty, no retry
                            fetched = coverage.to_fetch(session, cid)
                            for first, last in fetched:
                                if status is not None and status.get("stop"):
                                    with lock:
                                        flags["aborted"] = flags["stopped"] = True
                                    return
                                with gate.slot():
                                    if token is None:
                                        token = kronos.get_token(http, rep, cid, start, end, cfg)
                                        kronos.warm_up(tmb, token, cfg)
                                    part, cols_ = kronos.get_rows(tmb, rep, token, str(first), str(last), cfg)
                                rows += part
                                cols = cols or cols_
                                cfg.pause()
                        else:
                            for attempt in (1, 2):  # an empty result gets one retry on a fresh token
                                with gate.slot():
                                    if token is None:
                                        token = kronos.get_token(http, rep, cid, start, end, cfg)
                                        kronos.warm_up(tmb, token, cfg)
                                    rows, cols = kronos.get_rows(tmb, rep, token, start, end, cfg, session.granularity)
                                if rows:
                                    break
                                log.info("empty result, retrying: %s center %s (attempt %d)", rep_name, cid, attempt)
                                token = None
                                cfg.pause()
                        seconds = round(time.monotonic() - t0, 1)
                        gate.healthy(seconds)
                        if fetched == []:  # fill_missing and every day is already stored
                            save_section(session, cid, rep_name, [], cols, seconds)
                            with lock:
                                st["ok"] += 1
                            note(f"ok     {rep_name} · {cid} {label} · all days already stored")
                        elif not rows:
                            # empty after a retry: done for this session (a new session asks again)
                            if not SectionResult.objects.filter(session=session, center_id=cid, report=rep_name,
                                                                status=SectionResult.OK).exists():
                                record_result(session, cid, rep_name, SectionResult.EMPTY, 0, seconds)
                            with lock:
                                st["empty"].append(cid)
                            note(f"empty  {rep_name} · {cid} {label}")
                            if fetched:
                                coverage.mark(session, cid, fetched)
                        else:
                            if len(rows) >= row_cap:
                                with lock:
                                    st["capped"].append(cid)
                                log.warning("possible truncation: %s center %s has %d rows", rep_name, cid, len(rows))
                            save_section(session, cid, rep_name, rows, cols, seconds)
                            files.write_center_file(session, center, rep_name, cols, rows)
                            with lock:
                                st["ok"] += 1
                            note(f"ok     {rep_name} · {cid} {label} · {len(rows)} rows · {seconds}s")
                            if fetched:
                                coverage.mark(session, cid, fetched)
                        with lock:
                            fail_streak[rep_name] = 0
                            flags["auth_failures"] = 0
                    except kronos.AuthError as e:
                        record_result(session, cid, rep_name, SectionResult.FAILED, 0, None, str(e))
                        with lock:
                            st["failed"].append(cid)
                            flags["auth_failures"] += 1
                            if flags["auth_failures"] >= auth_limit:
                                flags["aborted"] = True
                        note(f"AUTH   {rep_name} · {cid} {e}")
                        mark(cid, rep_name, "failed")
                    except kronos.SlowQuery as e:
                        token = None
                        gate.struggling()
                        record_result(session, cid, rep_name, SectionResult.SLOW, 0,
                                      round(time.monotonic() - t0, 1), str(e))
                        with lock:
                            st["slow"].append(cid)
                        note(f"SLOW   {rep_name} · {cid} {label} · {e}")
                        mark(cid, rep_name, "slow")
                    except Exception as e:
                        token = None
                        gate.struggling()
                        record_result(session, cid, rep_name, SectionResult.FAILED, 0, None, str(e))
                        with lock:
                            st["failed"].append(cid)
                            fail_streak[rep_name] += 1
                            skip = fail_streak[rep_name] >= report_limit and st["ok"] == 0
                            if skip:
                                st["skipped"] = True
                        note(f"FAILED {rep_name} · {cid} {label} · {e}")
                        mark(cid, rep_name, "failed")
                        if skip:
                            log.error("report %s skipped: %d failures in a row, none succeeded", rep_name, report_limit)
                    mark(cid, rep_name, "done")
                    if status is not None:
                        status["workers"] = gate.limit
                    advance()
                    if flags["aborted"]:
                        return
                    cfg.pause()
        finally:
            tmb.close()
            if gate.maximum > 1:
                connection.close()  # this worker thread's own database connection

    if gate.maximum <= 1:
        for cid in ids:
            if flags["aborted"]:
                break
            process_center(cid)
    else:
        with ThreadPoolExecutor(max_workers=gate.maximum) as pool:
            for fut in [pool.submit(process_center, cid) for cid in ids]:
                fut.result()

    return _finish(session, ids, reports, stats, flags, status)


def register_known_empty(session):
    """Mark center/reports as known-empty once they were empty in two sessions.

    Only counts when the report returned data for other centers both times: a report that is
    empty for *everyone* (the sales card on an old period) says nothing about one center.
    """
    def has_data(s, report):
        return SectionResult.objects.filter(session=s, report=report, status=SectionResult.OK).exists()

    for r in SectionResult.objects.filter(session=session, status=SectionResult.EMPTY).exclude(
            error__startswith="known empty"):
        if not has_data(session, r.report):
            continue
        earlier = SectionResult.objects.filter(center_id=r.center_id, report=r.report,
                                               status=SectionResult.EMPTY).exclude(session=session)
        if any(has_data(e.session, r.report) for e in earlier.select_related("session")):
            KnownEmpty.objects.get_or_create(center_id=r.center_id, report=r.report,
                                             defaults={"note": "empty in two sessions"})


def _finish(session, ids, reports, stats, flags, status):
    register_known_empty(session)
    files.export_reports(session)
    coverage = files.write_coverage(session, stats)
    files.merge_session(session)

    saved = {(r, c) for r, c in SectionResult.objects.filter(
        session=session, status__in=DONE_STATES).values_list("report", "center_id")}
    missing = {r: [str(c) for c in ids if (r, c) not in saved] for r in reports}
    session.missing = {r: m for r, m in missing.items() if m}
    session.coverage = coverage
    session.checks = checks.run(session)
    try:
        customers.link_session(session)  # sales / gym rows -> customers, newest status wins
        masters.merge_session(session)   # the all-runs gym and expired-pack tables
        masters.prune_run_rows(config.KEEP_RUN_ROWS)
    except Exception:  # noqa: BLE001 - a linking problem must not fail a finished scrape; `link_customers` retries
        log.exception("could not link customers for %s", session.session_id)
    session.status = Session.COMPLETE if not session.missing else Session.INCOMPLETE
    session.finished_at = timezone.now()
    session.save()
    files.write_session_json(session)
    if status is not None:
        status.update(state="done", desc="", session_status=session.status,
                      stopped=flags["stopped"], aborted=flags["aborted"])
    return session
