"""django-q2 task: run one scrape job inside the `qcluster` process."""
import logging
import os
import threading
import traceback

from django.db import connection
from django.utils import timezone

from . import engine, kronos
from .models import Job, Session

log = logging.getLogger("scraper.tasks")
HEARTBEAT_EVERY = 1.0


def outcome(status):
    """(Job state, error text) for a run that returned: an early end is said in words, never a silent DONE."""
    reason = status.get("reason")
    if reason == "auth":
        limit = status.get("auth_limit") or 3
        return Job.ERROR, (f"Kronos login rejected {limit} times - log in on the Run page and press Continue "
                           "to finish the session.")
    if reason == "error":
        return Job.ERROR, "A center failed unexpectedly and the run was stopped (see the job log). Continue the session to finish it."
    if reason == "stop" or status.get("stopped"):
        return Job.DONE, "Stopped by you. Continue the session to fetch the rest."
    if status.get("problems"):
        return Job.DONE, "Scraped, but a step after the scrape failed: " + "; ".join(status["problems"])[:1500]
    return Job.DONE, ""


def run_job(job_id):
    status = {"state": "running", "log": [], "cells": {}, "active": [], "done": 0, "total": 0,
              "desc": "starting…", "stop": False, "pause": False, "workers": 1}
    finished = threading.Event()
    thread, session, claimed = None, None, False
    state, error = Job.DONE, ""

    def heartbeat():
        """Copy progress into the Job row and pick up Stop/Pause requests from the admin."""
        try:
            while not finished.wait(HEARTBEAT_EVERY):
                try:
                    row = Job.objects.filter(pk=job_id).values("stop_requested", "pause_requested", "state").first()
                    if row:
                        status["stop"], status["pause"] = row["stop_requested"], row["pause_requested"]
                    Job.objects.filter(pk=job_id).update(
                        heartbeat=timezone.now(), done=status.get("done", 0), total=status.get("total", 0),
                        desc=status.get("desc", "")[:300], log=list(status["log"])[-200:],
                        cells=dict(status["cells"]), active=list(status["active"]),
                        workers_now=status.get("workers", 1))
                    if row and row["state"] == Job.INTERRUPTED:  # wrongly written off after a stall: it is alive
                        Job.objects.filter(pk=job_id, state=Job.INTERRUPTED).update(state=Job.RUNNING, error="")
                        Session.objects.filter(pk=session.pk, status=Session.INTERRUPTED).update(status=Session.RUNNING)
                except Exception:  # noqa: BLE001 - one failed write (a lock, a sleep) must not end Stop / Pause for good
                    log.exception("heartbeat of job %s failed; trying again", job_id)
        finally:
            connection.close()

    try:  # everything inside: a failure even in the first lookup ends the Job as ERROR, never stuck "queued"
        job = Job.objects.select_related("session").get(pk=job_id)
        session, spec = job.session, job.spec
        # one atomic step: a job cancelled (or picked up twice) between the read and now is left alone
        if not Job.objects.filter(pk=job.pk, state=Job.QUEUED).update(
                state=Job.RUNNING, pid=os.getpid(), started_at=timezone.now(), heartbeat=timezone.now()):
            return
        claimed = True
        status["workers"] = spec.get("workers", 1)
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        engine.run_session(session, workers=spec.get("workers", 1), status=status,
                           refresh=spec.get("refresh", False), only=spec.get("only"),
                           adaptive=spec.get("adaptive", False), recheck_known=spec.get("recheck_known", False))
        state, error = outcome(status)
    except Exception as e:  # shown in the admin instead of dying silently
        log.error("job %s failed: %s", job_id, kronos.scrub(traceback.format_exc()))
        state, error = Job.ERROR, kronos.scrub(f"{e.__class__.__name__}: {e}")[:2000]
        if session is not None:
            Session.objects.filter(pk=session.pk).update(status=Session.INCOMPLETE)
        if not claimed:  # it failed before it was marked running: end it anyway
            Job.objects.filter(pk=job_id, state__in=(Job.QUEUED, Job.RUNNING)).update(
                state=Job.ERROR, error=error, finished_at=timezone.now())
    finally:
        finished.set()
        if thread is not None:
            thread.join(timeout=5)
        if claimed:
            Job.objects.filter(pk=job_id).update(
                state=state, error=error, finished_at=timezone.now(), heartbeat=timezone.now(),
                done=status.get("done", 0), total=status.get("total", 0), desc="",
                log=list(status["log"])[-200:], cells=dict(status["cells"]), active=[])
            _after(session, state, error)


def _after(session, state, error):
    """Tell the automatic run that started this session how it ended, then start one that was waiting."""
    from . import autorun
    try:
        if session is not None:
            autorun.job_finished(Session.objects.get(pk=session.pk), state, error)
        autorun.start_waiting()
    except Exception:  # noqa: BLE001 - bookkeeping must never turn a finished job into a failed one
        log.exception("could not update the automatic runs after a job")


def auto_run(pk):
    """django-q2 schedule entry point: start the automatic run `pk`."""
    from . import autorun
    from .models import AutoRun
    auto = AutoRun.objects.filter(pk=pk).first()
    if auto:
        autorun.run(auto)
