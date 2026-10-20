"""Second-round web checks: partly fetched months on Revenue, the range notice, safe export headers, labelled controls."""
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import TransactionTestCase

from centers import services as center_services
from scraper import auth, engine, revenue
from scraper.resources import CenterMonthResource
from scraper.testing import FakeKronosMixin

BASE = "/admin/console/"


class RevenuePartialTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        center_services.set_list("test", [1, 2, 3, 4])
        engine.run_session(engine.new_session("2026-07-01", "2026-07-31", [1, 2, 3, 4], list(engine.REPORTS)))
        patcher = mock.patch.object(auth, "logged_in", lambda fresh=False: True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_partly_fetched_past_month_is_marked_with_its_days(self):
        real = revenue.matrix

        def with_partial(*args, **kwargs):
            data = real(*args, **kwargs)
            past = [m for m in data["months"] if m < data["current"]]
            if data["rows"] and past:
                cid, month = data["rows"][0]["id"], past[-1]
                for r in data["rows"] + [r for sec in data["sections"] for r in sec["rows"]]:
                    if r["id"] == cid:
                        r["partial"] = {month: "10 of 31 days"}
            return data

        with mock.patch.object(revenue, "matrix", side_effect=with_partial):
            r = self.client.get(BASE + "revenue/", {"months": 6})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["data"]["has_data"])
        self.assertContains(r, "(10 of 31 days)")
        self.assertContains(r, "only partly fetched")  # revenue.PARTIAL_HELP explains what the mark means

    def test_a_range_without_months_says_why(self):
        r = self.client.get(BASE + "revenue/", {"from": "2001-01", "to": "2001-03"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "No months in this range")
        self.assertNotContains(r, "No monthly revenue yet")


class ExportHeaderTests(TransactionTestCase):
    def test_metric_column_names_are_made_safe(self):
        with mock.patch("scraper.perfwide.extra_metric_names", return_value=["=HYPERLINK(1)", "Rating"]):
            resource = CenterMonthResource()
        headers = resource.export(resource.get_queryset().none()).headers
        self.assertIn("'=HYPERLINK(1)", headers)
        self.assertIn("Rating", headers)
        self.assertNotIn("=HYPERLINK(1)", headers)


class DataTableLabelTests(TransactionTestCase):
    def test_search_and_page_size_have_labels(self):
        js = (Path(settings.BASE_DIR) / "console/static/console/datatable.js").read_text(encoding="utf-8")
        self.assertIn('search.setAttribute("aria-label"', js)
        self.assertIn('sizes.setAttribute("aria-label"', js)
