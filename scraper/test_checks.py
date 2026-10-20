from django.test import TransactionTestCase

from scraper import checks, engine
from scraper.models import Session
from scraper.testing import FakeKronosMixin


class CheckTests(FakeKronosMixin, TransactionTestCase):
    def run_period(self, reports=("sales",)):
        return engine.run_session(engine.new_session("2026-07-01", "2026-07-31", [1, 2, 3, 4], list(reports)))

    def test_a_clean_run_has_no_findings(self):
        self.assertEqual(self.run_period().checks, [])

    def test_active_center_without_sales_is_flagged(self):
        self.mode["empty2"] = True
        findings = self.run_period().checks
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["level"], "warning")
        self.assertIn("1 ACTIVE center(s) returned no sales rows: 2 Cult 2", findings[0]["text"])

    def test_big_change_since_the_previous_run_of_the_same_period_is_noted(self):
        self.mode["sales_rows"] = 10
        first = self.run_period()
        self.mode["sales_rows"] = 40
        second = self.run_period()
        text = " ".join(f["text"] for f in second.checks)
        self.assertIn(f"since session {first.pk}", text)
        self.assertIn("1 (10 → 40)", text)
        self.assertEqual(checks.run(first), [])  # nothing earlier to compare with

    def test_row_cap_hits_are_reported(self):
        session = self.run_period()
        Session.objects.filter(pk=session.pk).update(coverage=[{"report": "sales", "possibly_truncated": 2}])
        session.refresh_from_db()
        self.assertIn("2 result(s) hit the row cap", checks.run(session)[0]["text"])

    def test_findings_reach_the_home_alerts(self):
        from scraper import selectors
        self.mode["empty2"] = True
        self.run_period()
        texts = " ".join(a["text"] for a in selectors.home()["alerts"])
        self.assertIn("returned no sales rows", texts)
