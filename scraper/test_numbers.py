from django.test import TransactionTestCase
from django.urls import reverse

from scraper import engine, numbers, storage
from scraper.models import CardRow, GymMember, SalesRecord
from scraper.resources import GymMemberResource
from scraper.testing import FakeKronosMixin


class NumberTests(FakeKronosMixin, TransactionTestCase):
    def test_trim(self):
        self.assertEqual([numbers.trim(v) for v in (354012.54000000004, 1000.0, 0.8523, -0.00001, 601719.3600000001, 12)],
                         ["354012.54", "1000", "0.8523", "0", "601719.36", "12"])
        self.assertEqual(numbers.tidy([1.00000000001, {"a": [2.5000000001]}, "x", None]), [1.0, {"a": [2.5]}, "x", None])
        self.assertEqual(storage._f("354012.54000000004"), 354012.54)

    def make_row(self):
        session = engine.new_session("2026-07-01", "2026-07-31", [1], ["gym"])
        return GymMember.objects.create(session=session, center_id=1, user_id="U1", username="Asha",
                                        final_amount_paid=354012.54000000004), session

    def test_admin_lists_and_detail_pages_show_trimmed_numbers(self):
        row, session = self.make_row()
        SalesRecord.objects.create(session=session, center_id=1, user_id="U1", revenue=601719.3600000001)
        CardRow.objects.create(session=session, center_id=1, report="perf_x", row_num=0, data=["a", 12.30000000001])
        page = self.client.get(reverse("admin:scraper_gymmember_changelist"))
        self.assertContains(page, "354012.54")
        self.assertNotContains(page, "354012.54000000004")
        detail = self.client.get(reverse("admin:scraper_gymmember_change", args=[row.pk]))
        self.assertNotContains(detail, "354012.54000000004")
        self.assertContains(self.client.get(reverse("admin:scraper_salesrecord_changelist")), "601719.36<")
        self.assertContains(self.client.get(reverse("admin:scraper_cardrow_changelist")), "a, 12.3")

    def test_exports_are_trimmed_too(self):
        self.make_row()
        data = GymMemberResource().export()
        self.assertEqual(data.dict[0]["final_amount_paid"], "354012.54")

    def test_report_tables_have_grouped_headings(self):
        from scraper.models import PerfMetric
        import datetime as dt
        session = engine.new_session("2026-07-01", "2026-08-31", [1], ["perf"], granularity="month")
        for month in (dt.date(2026, 7, 1), dt.date(2026, 8, 1)):
            PerfMetric.objects.create(session=session, center_id=1, metric_date=month, metric="1. Total Revenue", value=10)
        page = self.client.get(reverse("console:revenue")).content.decode()
        self.assertIn('rowspan="2"', page)
        self.assertIn(">Revenue</th>", page)
        self.assertIn(">Ranking</th>", page)
        self.assertIn("By average", page)

    def test_serve_migrates_first(self):
        from unittest import mock
        from django.core.management import call_command
        with mock.patch("console.management.commands.serve.call_command") as called, \
                mock.patch("waitress.serve"), mock.patch("subprocess.Popen"), mock.patch("threading.Timer"):
            call_command("serve", "--no-open", "--no-jobs")
        self.assertEqual(called.call_args_list[0].args[0], "migrate")
