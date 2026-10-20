"""
Run a scrape from the command line (no browser, no job queue).

    python manage.py run_scrape --start 2026-09-20 --end 2026-10-19 --list test
    python manage.py run_scrape --session a1b2c3            # continue an unfinished session
    python manage.py run_scrape --start ... --end ... --only sales,perf --workers 2

Needs a valid Kronos login (the admin's Run page, or `python curefit_login.py`).
"""
import datetime as dt

from django.core.management.base import BaseCommand, CommandError

import curefit_login as C
from centers import services
from scraper import engine
from scraper.models import Session
from scraper.reports import REPORTS


class Command(BaseCommand):
    help = "Run (or continue) a scrape session."

    def add_arguments(self, p):
        p.add_argument("--start")
        p.add_argument("--end")
        p.add_argument("--list", default="actual", help="center list: actual (default) or test")
        p.add_argument("--only", help="comma-separated reports (default: all)")
        p.add_argument("--workers", type=int, default=1)
        p.add_argument("--session", help="continue this session id instead of starting a new one")
        p.add_argument("--refresh", action="store_true", help="re-fetch even what the session has")

    def handle(self, *a, **o):
        if not C.session_ok():
            raise CommandError("Not logged in to Kronos - log in first (admin Run page).")
        only = [x for x in (o["only"] or "").split(",") if x] or None
        bad = [r for r in (only or []) if r not in REPORTS]
        if bad:
            raise CommandError(f"Unknown report(s): {bad}. Known: {list(REPORTS)}")
        if o["session"]:
            try:
                session = Session.objects.get(session_id=o["session"])
            except Session.DoesNotExist:
                raise CommandError(f"No session {o['session']!r}")
        else:
            if not (o["start"] and o["end"]):
                raise CommandError("Give --start and --end (YYYY-MM-DD) or --session")
            start, end = dt.date.fromisoformat(o["start"]), dt.date.fromisoformat(o["end"])
            ids = services.list_ids(o["list"])
            if not ids:
                raise CommandError(f"The {o['list']} list is empty")
            session = engine.new_session(start, end, ids, only or list(REPORTS), workers=o["workers"])
        status = {}
        session = engine.run_session(session, workers=o["workers"], status=status,
                                     refresh=o["refresh"], only=only)
        self.stdout.write(f"session {session.session_id}: {session.status} -> {session.folder}")
        for line in status.get("log", [])[-15:]:
            self.stdout.write("  " + line)
