"""
Build the monthly performance table (one row per center and month, one column per metric).

    python manage.py merge_months

A finished scrape does this for its own centers and months; this builds it from everything scraped before.
"""
from django.core.management.base import BaseCommand

from scraper import perfwide
from scraper.models import CenterMonth


class Command(BaseCommand):
    help = "Rebuild the monthly performance table from the scraped perf rows."

    def handle(self, *args, **opts):
        written = perfwide.rebuild_months()
        self.stdout.write(f"{written} rows written; {CenterMonth.objects.count()} center-months.")
