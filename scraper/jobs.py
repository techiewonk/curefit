"""
Starting, watching, pausing and stopping scrape jobs (used by the admin pages).

A job is a `Job` row plus a django-q2 task. The task runs in the `qcluster` process, so a running
scrape survives web-server restarts. Progress, pause and stop all travel through the Job row.
"""
import os
import signal
import subprocess
import time
import uuid
from datetime import timedelta

from constance import config
from django.utils import timezone
from django_q.tasks import async_task

from centers import services
from centers.models import Center
from . import engine
from .models import Job, SectionResult, Session
from .reports import REPORTS

ACTIVE = (Job.QUEUED, Job.RUNNING)
STALE_AFTER = timedelta(seconds=45)  # no heartbeat for this long = the runner is gone


class JobError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def refresh_states():
    """A job that claims to run but has no heartbeat was interrupted (runner killed/restarted); one never picked up is dropped."""
    drop_unclaimed()
    now = timezone.now()
    for job in Job.objects.filter(state=Job.RUNNING):
        if job.heartbeat is None or now - job.heartbeat > STALE_AFTER:
            job.state, job.finished_at = Job.INTERRUPTED, now
            job.error = "The job runner stopped unexpectedly. Continue the session to finish it."
            job.save(update_fields=["state", "finished_at", "error"])
            if job.session_id:
                Session.objects.filter(pk=job.session_id, status=Session.RUNNING).update(
                    status=Session.INTERRUPTED)


def active_job():
    refresh_states()
    return Job.objects.filter(state__in=ACTIVE).order_by("-created_at").first()


def latest_job():
    refresh_states()
    return Job.objects.order_by("-created_at").first()


def enqueue(spec, sync=False):
    """Validate, create/continue the session, create the Job and queue it. Returns the Job."""
    if active_job():
        raise JobError("A scrape is already running", 409)
    only = spec.get("only") or None
    if only and not all(r in REPORTS for r in only):
        raise JobError("Unknown report in selection")
    workers = int(spec.get("workers") or 1)
    if not 1 <= workers <= config.MAX_WORKERS:
        raise JobError(f"workers must be between 1 and {config.MAX_WORKERS}")

    if spec.get("session"):
        session = Session.objects.filter(pk=spec["session"]).first()
        if session is None:
            raise JobError(f"Unknown session {spec['session']}", 404)
        if spec.get("retry_empty"):  # empties count as done; clearing them makes the run ask again
            SectionResult.objects.filter(session=session, status=SectionResult.EMPTY).delete()
    else:
        list_name = spec.get("list") or "actual"
        ids = sorted(Center.objects.filter(pk__in=spec["center_ids"]).values_list("pk", flat=True)) \
            if spec.get("center_ids") else services.list_ids(list_name)
        if not ids:
            raise JobError(f"The {list_name} center list is empty - pick centers in the admin first")
        if not spec.get("start") or not spec.get("end") or spec["start"] > spec["end"]:
            raise JobError("Give a valid period (start on or before end)")
        session = engine.new_session(spec["start"], spec["end"], ids, only or list(REPORTS),
                                     speed=spec.get("speed", "normal"), workers=workers,
                                     granularity=spec.get("granularity") or Session.MONTH,
                                     sales_chunk=spec.get("sales_chunk") or Session.WHOLE,
                                     fill_missing=spec.get("fill_missing"))

    job = Job.objects.create(
        id=time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4], session=session,
        spec={"only": only, "workers": workers, "refresh": bool(spec.get("refresh")),
              "adaptive": spec.get("speed") == "adaptive", "recheck_known": bool(spec.get("recheck_known"))})
    async_task("scraper.tasks.run_job", job.id, sync=sync, task_name=f"scrape {session.session_id}")
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
