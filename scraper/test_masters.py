import datetime as dt

from django.core.management import call_command
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from centers import services
from scraper import coverage, engine, masters
from scraper.models import GymMember, GymPack, SalesCoverage, SalesPack, SalesRecord, SectionResult, Session
from scraper.testing import FakeKronosMixin

D = dt.date
T0 = timezone.now()


class MergeTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.a = engine.new_session("2026-07-01", "2026-07-31", [1], ["gym"])
        self.b = engine.new_session("2026-08-01", "2026-08-31", [1], ["gym"])

    def gym(self, session, minutes, uid="U1", start=D(2026, 7, 1), end=D(2026, 7, 31), status="ACTIVE",
            last_class=D(2026, 7, 20), pack="Gold"):
        return GymMember.objects.create(
            session=session, center_id=1, user_id=uid, username="Asha", pack_name=pack, pack_start_date=start,
            pack_end_date=end, status=status, last_class_date=last_class, final_amount_paid=500.0,
            scraped_at=T0 + dt.timedelta(minutes=minutes))

    def test_new_packs_are_added_and_known_ones_updated(self):
        self.gym(self.a, 0)
        masters.merge_session(self.a)
        self.gym(self.b, 10, status="EXPIRED", end=D(2026, 9, 30), last_class=D(2026, 8, 25))  # extended pack
        self.gym(self.b, 10, uid="U2")
        masters.merge_session(self.b)
        self.assertEqual(GymPack.objects.count(), 2)  # the extension did not create a second row
        p = GymPack.objects.get(user_id="U1")
        self.assertEqual((p.status, p.pack_end_date, p.last_class_date, p.last_session_id),
                         ("EXPIRED", D(2026, 9, 30), D(2026, 8, 25), self.b.pk))

    def test_an_older_scrape_merged_late_cannot_undo_a_newer_one(self):
        self.gym(self.b, 10, status="EXPIRED", end=D(2026, 9, 30), last_class=D(2026, 8, 25))
        masters.merge_session(self.b)
        self.gym(self.a, 0, status="ACTIVE")  # scraped earlier, merged afterwards
        masters.merge_session(self.a)
        p = GymPack.objects.get()
        self.assertEqual((p.status, p.pack_end_date, p.last_class_date), ("EXPIRED", D(2026, 9, 30), D(2026, 8, 25)))

    def test_dates_never_go_back_even_for_a_newer_scrape(self):
        self.gym(self.a, 0, last_class=D(2026, 7, 28))
        masters.merge_session(self.a)
        self.gym(self.b, 10, last_class=D(2026, 7, 2))
        masters.merge_session(self.b)
        self.assertEqual(GymPack.objects.get().last_class_date, D(2026, 7, 28))

    def test_a_pack_missing_from_a_later_run_is_kept(self):
        self.gym(self.a, 0)
        masters.merge_session(self.a)
        masters.merge_session(self.b)  # nothing for U1 any more
        self.assertEqual(GymPack.objects.count(), 1)

    def test_sales_key_is_user_pack_and_end_date(self):
        for session, minutes, end, renewed in ((self.a, 0, D(2026, 7, 20), "No"), (self.b, 10, D(2026, 7, 20), "Yes"),
                                               (self.b, 10, D(2026, 8, 20), "No")):
            SalesRecord.objects.create(session=session, center_id=1, user_id="U1", pack_name="Gold",
                                       member_end_date=end, renewed=renewed, revenue=100.0,
                                       scraped_at=T0 + dt.timedelta(minutes=minutes))
        masters.merge_session(self.a)
        masters.merge_session(self.b)
        self.assertEqual(SalesPack.objects.count(), 2)  # a different end date is a different pack
        self.assertEqual(SalesPack.objects.get(member_end_date=D(2026, 7, 20)).renewed, "Yes")

    def test_prune_keeps_the_newest_sessions_and_only_merged_ones(self):
        SalesRecord.objects.create(session=self.a, center_id=1, user_id="U1", pack_name="Gold")
        SalesRecord.objects.create(session=self.b, center_id=1, user_id="U2", pack_name="Gold")
        self.assertEqual(masters.prune_run_rows(0), 0)
        self.assertEqual(masters.prune_run_rows(1), 0)  # session a is older but not merged yet
        masters.merge_session(self.a)
        self.assertEqual(masters.prune_run_rows(1), 1)
        self.assertEqual(list(SalesRecord.objects.values_list("user_id", flat=True)), ["U2"])
        self.assertEqual(SalesPack.objects.count(), 1)  # the data lives on

    def test_command_fills_the_tables(self):
        self.gym(self.a, 0)
        call_command("merge_masters")
        self.assertEqual(GymPack.objects.count(), 1)
        self.assertTrue(Session.objects.get(pk=self.a.pk).merged_into_masters)


class CoverageTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        services.set_list("test", [1, 2])
        self.today = timezone.localdate()
        self.start, self.end = self.today - dt.timedelta(days=3), self.today - dt.timedelta(days=1)

    def test_pieces(self):
        self.assertEqual(coverage.pieces(D(2026, 7, 1), D(2026, 7, 3), "whole"), [(D(2026, 7, 1), D(2026, 7, 3))])
        self.assertEqual(len(coverage.pieces(D(2026, 7, 1), D(2026, 7, 3), "day")), 3)
        self.assertEqual(coverage.pieces(D(2026, 7, 1), D(2026, 7, 10), "week"),
                         [(D(2026, 7, 1), D(2026, 7, 7)), (D(2026, 7, 8), D(2026, 7, 10))])

    def run_sales(self, **kw):
        s = engine.new_session(self.start, self.end, [1, 2], ["sales"], sales_chunk="day", **kw)
        return engine.run_session(s)

    def test_day_by_day_records_coverage_and_fill_missing_skips_stored_days(self):
        self.run_sales()
        asked = sorted({(p["start_date"], p["end_date"]) for p in self.params})
        self.assertEqual(len(asked), 3)  # one request per day, not one for the period
        self.assertTrue(all(a == b for a, b in asked))
        self.assertEqual(SalesCoverage.objects.count(), 6)  # 3 days x 2 centers
        self.params.clear()
        second = self.run_sales(fill_missing=True)
        self.assertEqual(self.params, [])  # nothing left to ask
        self.assertEqual(SectionResult.objects.filter(session=second, status=SectionResult.OK).count(), 2)
        SalesCoverage.objects.filter(center_id=1, day=self.start).delete()
        self.run_sales(fill_missing=True)
        self.assertEqual({(p["start_date"], p["end_date"]) for p in self.params},
                         {(str(self.start), str(self.start))})  # only the gap, only for the center that has it

    def test_days_outside_the_window_and_today_are_not_recorded(self):
        old = self.today - dt.timedelta(days=200)
        coverage.mark(Session(session_id="x"), 1, [(old, old), (self.today, self.today)])
        self.assertEqual(SalesCoverage.objects.count(), 0)

    def test_pages(self):
        self.run_sales()
        r = self.client.get(reverse("console:coverage"), {"list": "test"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["days"]), coverage.WINDOW_DAYS)
        self.assertEqual(self.client.get("/admin/console/run/", {"list": "test", "start": str(self.start)}).status_code, 200)
        for model in ("gympack", "salespack", "salescoverage"):
            self.assertEqual(self.client.get(reverse(f"admin:scraper_{model}_changelist")).status_code, 200, model)
            self.assertEqual(self.client.get(reverse(f"admin:scraper_{model}_export")).status_code, 200, model)
