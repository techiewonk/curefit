"""
Merge scraped sales and gym rows into the all-runs tables (one row per member pack).

    python manage.py merge_masters             # sessions not merged yet, oldest first
    python manage.py merge_masters --all       # every session again (safe to repeat)
    python manage.py merge_masters --prune     # also drop per-run rows of old sessions (KEEP_RUN_ROWS in Settings)

A finished scrape does this by itself; this fills the tables from sessions scraped before they existed.
"""
from constance import config
from django.core.management.base import BaseCommand

from scraper import masters
from scraper.models import GymPack, SalesPack, Session


class Command(BaseCommand):
    help = "Merge scraped rows into the all-runs gym and expired-pack tables."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="redo every session")
        parser.add_argument("--prune", action="store_true", help="delete per-run rows of old merged sessions")

    def handle(self, *args, **opts):
        sessions = Session.objects.order_by("end_date", "created_at")
        if not opts["all"]:
            sessions = sessions.filter(merged_into_masters=False)
        for session in sessions:
            result = masters.merge_session(session)
            self.stdout.write(f"{session.session_id}: gym {result['gym']} sales {result['sales']} (new, updated)")
        if opts["prune"]:
            self.stdout.write(f"Pruned {masters.prune_run_rows(config.KEEP_RUN_ROWS)} per-run rows.")
        self.stdout.write(f"{GymPack.objects.count()} gym packs, {SalesPack.objects.count()} expired packs.")
