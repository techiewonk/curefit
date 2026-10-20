"""
Starting, watching, pausing and stopping scrape jobs (used by the admin pages).

A job is a `Job` row plus a django-q2 task. The task runs in the `qcluster` process, so a running
scrape survives web-server restarts. Progress, pause and stop all travel through the Job row.
"""
import datetime as dt
import os
import signal
import subprocess
import time
import uuid
from datetime import timedelta

from constance import config
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django_q.tasks import async_task

from centers import services
from centers.models import Center
from . import auth, engine
from .conf import SPEED_PROFILES
from .models import Job, SectionResult, Session
from .reports import REPORTS

ACTIVE = (Job.QUEUED, Job.RUNNING)
STALE_AFTER = timedelta(seconds=45)  # no heartbeat for this long and its process is gone = the runner is gone
HUNG_AFTER = timedelta(minutes=15)   # no heartbeat for this long = written off even if a process with its pid exists
ORPHAN_AFTER = timedelta(minutes=30)  # a "running" session with no job and nothing saved for this long was cut off
MAX_DAYS = 400         # longest period one run may ask for, unless confirmed
MAX_DAYS_BY_DAY = 120  # the same when the sales card is asked for one day at a time (one request per day and gym)


def pid_alive(pid):
    """True if a process with this id exists (the job runner's worker that claims a job)."""
    if not pid:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # access denied: it exists, it is just not ours
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class JobError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def refresh_states():
    """A job that claims to run but has no heartbeat and whose process is gone was interrupted (runner killed or
    restarted); one never picked up is dropped; a session left "running" by a run that was cut off is interrupted."""
    drop_unclaimed()
    now = timezone.now()
    for job in Job.objects.filter(state=Job.RUNNING):
        silent = now - job.heartbeat if job.heartbeat else None
        if silent is not None and (silent <= STALE_AFTER or (silent <= HUNG_AFTER and pid_alive(job.pid))):
            continue  # beating, or only stalled (a busy database) while its process is still there
        job.state, job.finished_at = Job.INTERRUPTED, now
        job.error = "The job runner stopped unexpectedly. Continue the session to finish it."
        job.save(update_fields=["state", "finished_at", "error"])
        if job.session_id:
            Session.objects.filter(pk=job.session_id, status=Session.RUNNING).update(
                status=Session.INTERRUPTED)
        _tell_autorun(job)
    cutoff = now - ORPHAN_AFTER
    for session in Session.objects.filter(status=Session.RUNNING, created_at__lt=cutoff).exclude(jobs__state__in=ACTIVE):
        last = SectionResult.objects.filter(session=session).aggregate(at=Max("scraped_at"))["at"]
        if last is None or last < cutoff:
            Session.objects.filter(pk=session.pk, status=Session.RUNNING).update(status=Session.INTERRUPTED)


def active_job():
    refresh_states()
    return Job.objects.filter(state__in=ACTIVE).order_by("-created_at").first()


def latest_job():
    refresh_states()
    return Job.objects.order_by("-created_at").first()


def _date(value, what):
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value or ""))
    except ValueError:
        raise JobError(f"Give the {what} date as YYYY-MM-DD (got {str(value)[:20]!r})") from None


def _choice(spec, key, choices, default, what):
    value = spec.get(key) or default
    if value not in choices:
        raise JobError(f"Unknown {what} {str(value)[:20]!r}: use one of {', '.join(choices)}")
    return value


def _validate(spec):
    """The checked settings of a run: (only, workers, speed, granularity, sales_chunk). Raises JobError."""
    only = spec.get("only")
    if only is not None:
        only = list(only)
        if not only:
            raise JobError("Pick at least one report")
        if not all(r in REPORTS for r in only):
            raise JobError("Unknown report in selection")
    try:
        workers = int(spec.get("workers") or 1)
    except (TypeError, ValueError):
        raise JobError("workers must be a whole number") from None
    workers = max(1, min(workers, config.MAX_WORKERS))  # more than the Max workers setting allows: use the maximum
    speed = _choice(spec, "speed", list(SPEED_PROFILES), "normal", "speed")
    granularity = _choice(spec, "granularity", [c for c, _ in Session.GRANULARITY_CHOICES], Session.MONTH, "split")
    sales_chunk = _choice(spec, "sales_chunk", [c for c, _ in Session.CHUNK_CHOICES], Session.WHOLE, "sales chunk")
    return only, workers, speed, granularity, sales_chunk


def _period(spec, only, sales_chunk):
    start, end = _date(spec.get("start"), "start"), _date(spec.get("end"), "end")
    if start > end:
        raise JobError("Give a valid period (start on or before end)")
    by_day = sales_chunk == Session.DAY and (only is None or "sales" in only)
    limit = MAX_DAYS_BY_DAY if by_day else MAX_DAYS
    days = (end - start).days + 1
    if days > limit and not spec.get("confirm_long"):
        raise JobError(f"The period is {days} days long; one run may ask for at most {limit} days"
                       f"{' when sales are fetched one day at a time' if by_day else ''}. "
                       "Split it into shorter runs (or confirm the long run).")
    return start, end


def enqueue(spec, sync=False):
    """Validate, create/continue the session, create the Job and queue it. Returns the Job.

    The "one scrape at a time" check and the new Job are one database transaction (taken with the write lock),
    so two clicks at once cannot start two scrapes. Raises JobError with a message for the page."""
    only, workers, speed, granularity, sales_chunk = _validate(spec)
    if not auth.logged_in(fresh=True):
        raise JobError(auth.not_logged_in_message(), 403)
    with transaction.atomic():
        if active_job():
            raise JobError("A scrape is already running", 409)
        if spec.get("session"):
            session = Session.objects.filter(pk=spec["session"]).first()
            if session is None:
                raise JobError(f"Unknown session {spec['session']}", 404)
        else:
            list_name = spec.get("list") or "actual"
            if spec.get("center_ids"):
                try:
                    picked = [int(c) for c in spec["center_ids"]]
                except (TypeError, ValueError):
                    raise JobError("Center ids must be whole numbers") from None
                ids = sorted(Center.objects.filter(pk__in=picked).values_list("pk", flat=True))
            else:
                ids = services.list_ids(list_name)
            if not ids:
                raise JobError(f"The {list_name} center list is empty - pick centers in the admin first")
            start, end = _period(spec, only, sales_chunk)
            session = engine.new_session(start, end, ids, only or list(REPORTS), speed=speed, workers=workers,
                                         granularity=granularity, sales_chunk=sales_chunk,
                                         fill_missing=spec.get("fill_missing"))

        job = Job.objects.create(
            id=time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4], session=session,
            spec={"only": only, "workers": workers, "refresh": bool(spec.get("refresh")),
                  "adaptive": speed == "adaptive", "recheck_known": bool(spec.get("recheck_known"))})
        if not sync:  # queued in the same transaction: no Job without its task, no task without its Job
            async_task("scraper.tasks.run_job", job.id, task_name=f"scrape {session.session_id}")
        if spec.get("session") and spec.get("retry_empty"):  # last: empties count as done, clearing them asks again
            SectionResult.objects.filter(session=session, status=SectionResult.EMPTY).delete()
    if sync:  # runs here and now (tests, the command line): outside the transaction, the scrape commits as it goes
        async_task("scraper.tasks.run_job", job.id, sync=True, task_name=f"scrape {session.session_id}")
    return job


PICKUP_MINUTES = 10  # the runner takes a queued job within seconds; far longer means it is not running


def drop_unclaimed():
    """Cancel a job that has been queued so long that no job runner can be running (it would block every other run)."""
    cutoff = timezone.now() - timedelta(minutes=PICKUP_MINUTES)
    for job in Job.objects.filter(state=Job.QUEUED, created_at__lt=cutoff):
        cancel(job)
        Job.objects.filter(pk=job.pk).update(
            error=f"Not picked up within {PICKUP_MINUTES} minutes: the job runner is not running. Start it (run.bat) and queue again.")


def request_stop(job):
    """Stop a job. Queued: cancelled at once. Running: it ends cleanly after the requests in flight."""
    if job.state == Job.QUEUED:
        return cancel(job)
    if job.state == Job.RUNNING:
        Job.objects.filter(pk=job.pk).update(stop_requested=True, pause_requested=False)


def cancel(job):
    """A job that has not started (for instance the job runner is not running): it never will."""
    now = timezone.now()
    if Job.objects.filter(pk=job.pk, state=Job.QUEUED).update(
            state=Job.CANCELLED, finished_at=now, error="Cancelled before it started."):
        if job.session_id:  # a session nothing ran in is just waiting to be continued
            Session.objects.filter(pk=job.session_id, status=Session.RUNNING).update(status=Session.INCOMPLETE)


def force_stop(job):
    """Stop right now: end the job runner's worker even if it is waiting on a slow Metabase request.

    The session is left 'interrupted' (continue it to finish); what was already saved is kept.
    """
    job.refresh_from_db()
    if job.state != Job.RUNNING:
        return request_stop(job)
    Job.objects.filter(pk=job.pk).update(stop_requested=True)
    _kill(job.pid)
    Job.objects.filter(pk=job.pk).update(
        state=Job.INTERRUPTED, finished_at=timezone.now(), pause_requested=False,
        error="Stopped immediately: the request in progress was abandoned. Continue the session to finish it.")
    if job.session_id:
        Session.objects.filter(pk=job.session_id, status=Session.RUNNING).update(status=Session.INTERRUPTED)
    job.refresh_from_db()
    _tell_autorun(job)


def _tell_autorun(job):
    """A job that ended without its task finishing (runner killed, Stop now) never reaches tasks._after: the
    automatic run that started it would say 'started' for ever. Best effort, never raises."""
    if not job.session_id:
        return
    from . import autorun  # autorun imports jobs
    try:
        autorun.job_finished(Session.objects.get(pk=job.session_id), job.state, job.error)
    except Exception:  # noqa: BLE001 - bookkeeping only
        import logging
        logging.getLogger("scraper.jobs").exception("could not update the automatic run of job %s", job.pk)


def _kill(pid):
    if not pid or pid == os.getpid():
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def stop_active(force=False):
    """Stop whatever is running or queued. Returns the job it acted on (or None)."""
    job = active_job()
    if job:
        (force_stop if force else request_stop)(job)
    return job


def set_pause(job, on):
    if job.state not in ACTIVE:
        raise JobError("No scrape is running", 409)
    Job.objects.filter(pk=job.pk).update(pause_requested=bool(on))


def payload(job):
    """What the admin pages need to draw a job."""
    if job is None:
        return {"state": "idle", "log": [], "done": 0, "total": 0, "desc": "", "error": None,
                "session": None}
    job.refresh_from_db()
    return {"id": job.id, "state": job.state, "session": job.session_id, "done": job.done,
            "total": job.total, "desc": job.desc, "error": job.error or None, "log": job.log[-60:],
            "paused": job.pause_requested and job.state == Job.RUNNING,
            "stopping": job.stop_requested and job.state in ACTIVE, "workers": job.workers_now,
            "started_at": job.started_at.timestamp() if job.started_at else None,
            "cells": job.cells, "active": job.active,
            "session_status": job.session.status if job.session_id else None}
