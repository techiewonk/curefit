"""Automatic runs: a django-q2 schedule starts a scrape by itself — daily for the days up to yesterday,
weekly for last week, monthly for last month — so a report is ready each morning.

The Kronos login must still be valid at that moment; if it is not, the run is skipped and the reason is
shown on Home. The job runner (`manage.py serve` / run.bat) has to be running at the time.
"""
import datetime as dt

from croniter import croniter
from django.utils import timezone
from django_q.models import Schedule

from centers import services

from . import auth, coverage, jobs
from .conf import SPEED_PROFILES
from .models import AutoRun, Job, Session
from .reports import REPORTS

TASK = "scraper.tasks.auto_run"
SCHEDULE_TYPE = {AutoRun.DAILY: Schedule.DAILY, AutoRun.WEEKLY: Schedule.WEEKLY,
                 AutoRun.MONTHLY: Schedule.MONTHLY, AutoRun.CRON: Schedule.CRON}
ONE_DAY = dt.timedelta(days=1)


def period(auto, today=None):
    """(first day, last day) this campaign reports on, from its period rule."""
    today = today or timezone.localdate()
    yesterday = today - ONE_DAY
    rule = auto.period_rule
    if rule == AutoRun.PREV_WEEK:  # the week before this one, Monday to Sunday
        monday = today - dt.timedelta(days=today.weekday())
        return monday - dt.timedelta(days=7), monday - ONE_DAY
    if rule == AutoRun.PREV_MONTH:  # the calendar month before this one
        end = today.replace(day=1) - ONE_DAY
        return end.replace(day=1), end
    if rule == AutoRun.MONTH_TO_DATE:  # the 1st of the month yesterday is in, up to yesterday
        return yesterday.replace(day=1), yesterday
    if rule == AutoRun.FIXED and auto.start_date and auto.end_date:
        return auto.start_date, auto.end_date
    if rule == AutoRun.MISSING_SALES:  # the whole 90-day window; only the days not stored yet are fetched
        return coverage.window_start(today), yesterday
    return yesterday - dt.timedelta(days=auto.days - 1), yesterday


def center_ids(auto):
    """The gyms of this campaign: its center list, plus the gyms of its groups, plus the gyms picked by hand."""
    ids = set()
    if auto.centers_id:
        ids.update(services.list_ids(auto.centers.name))
    for group in auto.groups.all():
        ids.update(group.centers.values_list("pk", flat=True))
    ids.update(auto.gyms.values_list("pk", flat=True))
    return sorted(ids)


def summary(auto):
    """One line describing what the campaign does (for the list and the messages)."""
    start, end = period(auto)
    parts = [dict(AutoRun.PERIOD_CHOICES)[auto.period_rule].split(" (")[0], f"{start} to {end}"]
    return " · ".join(parts)


def next_occurrence(auto, now=None):
    """When the schedule should fire next (today's time if it has not passed yet)."""
    now = timezone.localtime(now or timezone.now())
    today = now.date()

    def at(day):
        return timezone.make_aware(dt.datetime.combine(day, auto.run_at))

    if auto.frequency == AutoRun.CRON:
        return croniter(auto.cron, now).get_next(dt.datetime)
    if auto.frequency == AutoRun.WEEKLY:
        candidate = at(today + dt.timedelta(days=(auto.weekday - today.weekday()) % 7))
        return candidate if candidate > now else candidate + dt.timedelta(days=7)
    if auto.frequency == AutoRun.MONTHLY:
        candidate = at(today.replace(day=auto.month_day))
        if candidate > now:
            return candidate
        return at((today.replace(day=1) + dt.timedelta(days=32)).replace(day=auto.month_day))
    candidate = at(today)
    return candidate if candidate > now else candidate + ONE_DAY


def schedule_name(auto):
    return f"autorun-{auto.pk}"


def fits(auto, when):
    """True if `when` is a moment this AutoRun's timing could fire at (so a planned next run can be kept)."""
    local = timezone.localtime(when)
    if auto.frequency == AutoRun.CRON:
        return bool(auto.cron) and croniter.is_valid(auto.cron) and croniter.match(auto.cron, local)
    if local.time().replace(second=0, microsecond=0) != auto.run_at.replace(second=0, microsecond=0):
        return False
    if auto.frequency == AutoRun.WEEKLY:
        return local.weekday() == auto.weekday
    if auto.frequency == AutoRun.MONTHLY:
        return local.day == auto.month_day
    return True


def sync_schedule(auto):
    """Keep the django-q2 schedule in step with the AutoRun (it exists while the run is enabled).

    The planned next run is only recalculated when the schedule is new or its timing changed: saving a campaign
    for another reason (its gyms, its reports) keeps a run that is due, even one that is overdue."""
    if not auto.enabled:
        return remove_schedule(auto)
    stype, cron = SCHEDULE_TYPE[auto.frequency], auto.cron if auto.frequency == AutoRun.CRON else None
    existing = Schedule.objects.filter(name=schedule_name(auto)).first()
    defaults = {"func": TASK, "args": f"({auto.pk},)", "schedule_type": stype, "repeats": -1, "cron": cron}
    keep = existing is not None and existing.next_run is not None and existing.schedule_type == stype \
        and (existing.cron or None) == cron and fits(auto, existing.next_run)
    if not keep:
        defaults["next_run"] = next_occurrence(auto)
    Schedule.objects.update_or_create(name=schedule_name(auto), defaults=defaults)


def remove_schedule(auto):
    Schedule.objects.filter(name=schedule_name(auto)).delete()


def next_run(auto):
    schedule = Schedule.objects.filter(name=schedule_name(auto)).first() if auto.enabled else None
    return schedule.next_run if schedule else None


def run(auto, force=False, inline=False):
    """Start this automatic run. Returns (status, message); both are also kept on the AutoRun.

    force=True is the "Run now" button: it ignores the on/off switch and does not wait for another scrape.
    inline=True does the whole scrape in this process (for `manage.py auto_run --wait`, started by
    Task Scheduler or cron) instead of queueing it for the job runner.

    last_status afterwards: started (queued; becomes finished / incomplete / failed when the job ends),
    waiting (another scrape was running: it starts by itself when that one ends), skipped or failed.
    """
    start, end = period(auto)
    if not auto.enabled and not force:
        return _record(auto, "skipped", "Switched off.")
    if not force and auto.last_status == "started" and auto.last_session_id:
        jobs.refresh_states()  # a job whose runner died no longer counts as going
        if Job.objects.active().filter(session_id=auto.last_session_id).exists():
            return "skipped", "The last start of this run is still going: not started twice."  # row left as it is
    ids = center_ids(auto)
    if not ids:
        return _record(auto, "failed", "No gyms: pick a center list, groups or gyms for this campaign.")
    if not auth.logged_in(fresh=True):
        if auth.last_state() == auth.UNREACHABLE:
            return _record(auto, "skipped", "Kronos could not be reached (offline, or Kronos is down): "
                                            "not started. It runs again at its next time, or press Run now.")
        return _record(auto, "skipped", "Not logged in to Kronos: log in on the Run page, then run again.")
    if jobs.active_job():
        if force or inline:
            return _record(auto, "skipped", "Another scrape is still running.")
        return _record(auto, "waiting", "Waiting: another scrape is running; this run starts by itself when it ends.")
    missing = auto.period_rule == AutoRun.MISSING_SALES
    reports = ["sales"] if missing else auto.reports
    spec = {"start": start.isoformat(), "end": end.isoformat(), "list": auto.centers.name if auto.centers_id else "campaign",
            "center_ids": ids, "only": None if set(reports) == set(REPORTS) else reports,
            "workers": SPEED_PROFILES[auto.speed], "speed": auto.speed, "granularity": auto.granularity,
            "sales_chunk": Session.DAY if missing else auto.sales_chunk, "fill_missing": True if missing else auto.fill_missing,
            "retry_empty": auto.retry_empty, "recheck_known": auto.recheck_known}
    unfinished = _unfinished(auto, start, end) if auto.continue_unfinished else None
    if unfinished:  # the last run of this campaign for this period did not finish: pick it up
        spec = {"session": unfinished.pk, "workers": spec["workers"], "speed": spec["speed"],
                "retry_empty": spec["retry_empty"], "recheck_known": spec["recheck_known"]}
    if inline:
        return _run_inline(auto, spec, start, end)
    try:
        job = jobs.enqueue(spec)
    except jobs.JobError as e:
        return _record(auto, "failed", str(e))
    what = "Continued" if unfinished else "Queued"
    return _record(auto, "started", f"{what} session {job.session_id} for {start} to {end} ({len(ids)} gyms).", job.session)


def _unfinished(auto, start, end):
    """This campaign's last session, if it is for the same period and did not complete."""
    last = auto.last_session
    if last and last.start_date == start and last.end_date == end and last.status != Session.COMPLETE:
        return last
    return None


def _record(auto, status, message, session=None, keep_time=False):
    """Keep the outcome on the AutoRun. Without a session the last one is left in place (so an unfinished
    session can still be continued after a skipped or failed start)."""
    fields = {"last_status": status, "last_message": message[:300]}
    if session is not None:
        fields["last_session"] = session
    if not keep_time:
        fields["last_run_at"] = timezone.now()
    AutoRun.objects.filter(pk=auto.pk).update(**fields)  # update(): no save(), so the schedule is not reset
    auto.refresh_from_db()
    return status, message


def _ended(session, job_state, error=""):
    """(last_status, message) for a session whose job has ended."""
    if job_state == Job.ERROR:
        status = "failed"
    else:
        status = "finished" if session.status == Session.COMPLETE else "incomplete"
    message = f"Session {session.session_id} {session.status}: {session.missing_count} center/section pairs missing."
    return status, (f"{message} {error}" if error else message)


def job_finished(session, job_state, error=""):
    """Called when a job ends: the AutoRun that started this session shows how it ended (not 'started' forever)."""
    status, message = _ended(session, job_state, error)
    AutoRun.objects.filter(last_session=session, last_status="started").update(
        last_status=status, last_message=message[:300])


def start_waiting():
    """Start the automatic runs that were waiting for another scrape to end (one at a time)."""
    for auto in AutoRun.objects.filter(enabled=True, last_status="waiting").order_by("last_run_at"):
        if jobs.active_job():
            return
        run(auto)


def _run_inline(auto, spec, start, end):
    """The whole scrape in this process, through a Job like any other run (one scrape at a time, retry-empty, a
    session left 'running' by a killed process is seen as interrupted)."""
    _record(auto, "started", f"Running in this process for {start} to {end}.")
    try:
        job = jobs.enqueue(spec, sync=True)
    except jobs.JobError as e:
        return _record(auto, "failed", str(e))
    job.refresh_from_db()
    session = Session.objects.get(pk=job.session_id)
    return _record(auto, *_ended(session, job.state, job.error), session=session)
