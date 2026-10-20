"""
Tie sales and gym rows to customers (one person per Kronos user id, phones as the fallback).

    python manage.py link_customers             # sessions that still have unlinked rows, oldest first
    python manage.py link_customers --all       # every session again (safe to repeat)
    python manage.py link_customers --leads     # only recompute lead status from the pack end dates

A finished scrape does this by itself; this fills in sessions scraped before customers existed.
"""
from django.core.management.base import BaseCommand

from scraper import customers
from scraper.models import Customer, Session


class Command(BaseCommand):
    help = "Link scraped rows to customers and refresh lead status."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="redo every session, not only unlinked ones")
        parser.add_argument("--leads", action="store_true", help="only refresh the lead status")

    def handle(self, *args, **opts):
        if opts["leads"]:
            self.stdout.write(f"Lead status refreshed for {customers.refresh_leads()} customers.")
            return
        if opts["all"]:
            for session in Session.objects.order_by("end_date", "created_at"):
                self.stdout.write(f"{session.session_id}: {customers.link_session(session)} customers updated")
        else:
            customers.link_pending()
        self.stdout.write(f"{Customer.objects.count()} customers.")
