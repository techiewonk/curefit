"""
Run a scrape from the command line (no browser, no job runner needed).

    python manage.py run_scrape --start 2026-09-20 --end 2026-10-19 --list test
    python manage.py run_scrape --session a1b2c3            # continue an unfinished session
    python manage.py run_scrape --start ... --end ... --only sales,perf --workers 2 --speed adaptive
    python manage.py run_scrape --start ... --end ... --only sales --sales-chunk day --granularity day

It runs as a normal job in this process: it shows on the Run page, Stop works, and it refuses to start
while another scrape is running. Needs a valid Kronos login (the admin's Run page).

Exit code: 0 = the session is complete, 1 = something is missing (continue it with --session) or it failed.
"""
import datetime as dt

from django.core.management.base import BaseCommand, CommandError

from scraper import jobs
from scraper.conf import SPEED_PROFILES
from scraper.models import Job, Session
from scraper.reports import REPORTS


def _day(value, name):
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise CommandError(f"--{name} must be a date like 2026-07-31 (got {value!r})") from None


class Command(BaseCommand):
    help = "Run (or continue) a scrape session in this process. Exit code 0 = complete, 1 = incomplete or failed."

    def add_arguments(self, p):
        p.add_argument("--start", help="first day, YYYY-MM-DD")
        p.add_argument("--end", help="last day, YYYY-MM-DD")
        p.add_argument("--list", default="actual", help="center list: actual (default) or test")
        p.add_argument("--only", help=f"comma-separated reports (default: all). Known: {', '.join(REPORTS)}")
        p.add_argument("--workers", type=int, default=1, help="centers at a time (capped at the Max workers setting)")
        p.add_argument("--speed", choices=list(SPEED_PROFILES), default="normal",
                       help="adaptive slows down and speeds up by itself")
        p.add_argument("--granularity", choices=[c for c, _ in Session.GRANULARITY_CHOICES], default=Session.MONTH,
                       help="how the perf cards split the period (default month)")
        p.add_argument("--sales-chunk", choices=[c for c, _ in Session.CHUNK_CHOICES], default=Session.WHOLE,
                       help="ask for Expired Pack User Details whole, a day or a week at a time")
        p.add_argument("--fill-missing", action="store_true", help="sales by day/week: only the days not stored yet")
        p.add_argument("--long", action="store_true", help="allow a period longer than the safety limit")
        p.add_argument("--session", help="continue this session id instead of starting a new one")
        p.add_argument("--refresh", action="store_true", help="re-fetch even what the session has")
        p.add_argument("--retry-empty", action="store_true", help="with --session: ask again for empty results")

    def handle(self, *a, **o):
        only = [x.strip() for x in (o["only"] or "").split(",") if x.strip()] or None
        bad = [r for r in (only or []) if r not in REPORTS]
        if bad:
            raise CommandError(f"Unknown report(s): {bad}. Known: {list(REPORTS)}")
        spec = {"only": only, "workers": o["workers"], "speed": o["speed"], "refresh": o["refresh"]}
        if o["session"]:
            spec.update(session=o["session"], retry_empty=o["retry_empty"])
        else:
            if not (o["start"] and o["end"]):
                raise CommandError("Give --start and --end (YYYY-MM-DD) or --session")
            start, end = _day(o["start"], "start"), _day(o["end"], "end")
            if start > end:
                raise CommandError("--start must be on or before --end")
            spec.update(start=start.isoformat(), end=end.isoformat(), list=o["list"], granularity=o["granularity"],
                        sales_chunk=o["sales_chunk"], fill_missing=o["fill_missing"], confirm_long=o["long"])
        try:
            job = jobs.enqueue(spec, sync=True)
        except jobs.JobError as e:
            raise CommandError(str(e))
        job.refresh_from_db()
        session = Session.objects.get(pk=job.session_id)
        self.stdout.write(f"session {session.session_id}: {session.status} -> {session.folder}")
        if job.error:
            self.stdout.write(f"  {job.error}")
        for line in (job.log or [])[-15:]:
            self.stdout.write("  " + line)
        if session.status != Session.COMPLETE or job.state == Job.ERROR:
            self.stdout.write(f"  continue with: python manage.py run_scrape --session {session.session_id}")
            raise SystemExit(1)
