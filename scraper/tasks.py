"""django-q2 task: run one scrape job inside the `qcluster` process."""
import logging
import os
import threading

from django.db import connection
from django.utils import timezone

from . import engine
from .models import Job, Session

log = logging.getLogger("scraper.tasks")
HEARTBEAT_EVERY = 1.0


def run_job(job_id):
    job = Job.objects.select_related("session").get(pk=job_id)
    session, spec = job.session, job.spec
    # one atomic step: a job cancelled (or picked up twice) between the read and now is left alone
    if not Job.objects.filter(pk=job.pk, state=Job.QUEUED).update(
            state=Job.RUNNING, pid=os.getpid(), started_at=timezone.now(), heartbeat=timezone.now()):
        return
    status = {"state": "running", "log": [], "cells": {}, "active": [], "done": 0, "total": 0,
              "desc": "starting…", "stop": False, "pause": False, "workers": spec.get("workers", 1)}
    finished = threading.Event()

    def heartbeat():
        """Copy progress into the Job row and pick up Stop/Pause requests from the admin."""
        try:
            while not finished.wait(HEARTBEAT_EVERY):
                try:
                    row = Job.objects.filter(pk=job.pk).values("stop_requested", "pause_requested", "state").first()
                    if row:
                        status["stop"], status["pause"] = row["stop_requested"], row["pause_requested"]
                    Job.objects.filter(pk=job.pk).update(
                        heartbeat=timezone.now(), done=status.get("done", 0), total=status.get("total", 0),
                        desc=status.get("desc", "")[:300], log=list(status["log"])[-200:],
                        cells=dict(status["cells"]), active=list(status["active"]),
                        workers_now=status.get("workers", 1))
                    if row and row["state"] == Job.INTERRUPTED:  # wrongly written off after a stall: it is alive
                        Job.objects.filter(pk=job.pk, state=Job.INTERRUPTED).update(state=Job.RUNNING, error="")
                        Session.objects.filter(pk=session.pk, status=Session.INTERRUPTED).update(status=Session.RUNNING)
                except Exception:  # noqa: BLE001 - one failed write (a lock, a sleep) must not end Stop / Pause for good
                    log.exception("heartbeat of job %s failed; trying again", job.pk)
        finally:
            connection.close()

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    state, error = Job.DONE, ""
    try:
        engine.run_session(session, workers=spec.get("workers", 1), status=status,
                           refresh=spec.get("refresh", False), only=spec.get("only"),
                           adaptive=spec.get("adaptive", False), recheck_known=spec.get("recheck_known", False))
    except Exception as e:  # shown in the admin instead of dying silently
        log.exception("job %s failed", job.pk)
        state, error = Job.ERROR, str(e)
        Session.objects.filter(pk=session.pk).update(status=Session.INCOMPLETE)
    finally:
        finished.set()
        thread.join(timeout=5)
        Job.objects.filter(pk=job.pk).update(
            state=state, error=error, finished_at=timezone.now(), heartbeat=timezone.now(),
            done=status.get("done", 0), total=status.get("total", 0), desc="",
            log=list(status["log"])[-200:], cells=dict(status["cells"]), active=[])


def auto_run(pk):
    """django-q2 schedule entry point: start the automatic run `pk`."""
    from . import autorun
    from .models import AutoRun
    auto = AutoRun.objects.filter(pk=pk).first()
    if auto:
        autorun.run(auto)
