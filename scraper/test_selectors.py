from datetime import timedelta
from unittest import mock

from django.test import TransactionTestCase
from django.utils import timezone

from centers import services
from scraper import auth, engine, selectors
from scraper.models import Job, SectionResult, Session
from scraper.testing import FakeKronosMixin


class SelectorTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.session = engine.run_session(
            engine.new_session("2026-07-01", "2026-07-31", [1, 2, 3, 4], list(engine.REPORTS)))

    def test_estimate_uses_measured_averages(self):
        SectionResult.objects.filter(report="sales").update(seconds=10)
        e = selectors.estimate(4, ["sales"], 2)
        self.assertEqual((e["centers"], e["reports"], e["pairs"], e["workers"]), (4, 1, 4, 2))
        self.assertEqual(e["seconds_low"], round((10 + selectors.PAIR_OVERHEAD) * 4 / 2))
        self.assertEqual(e["seconds_high"], round(e["seconds_low"] * selectors.HIGH_FACTOR))
        self.assertEqual(e["assumed"], [])

    def test_estimate_assumes_forty_seconds_for_untimed_reports(self):
        SectionResult.objects.filter(report="perf_ratings").update(seconds=None)
        e = selectors.estimate(3, ["perf_ratings"], 1)
        self.assertEqual(e["assumed"], ["perf_ratings"])
        self.assertEqual(e["seconds_low"], round(selectors.UNKNOWN_SECONDS * 3))
        self.assertIn("minute", e["low_text"])

    def test_monitor_grid_reflects_results(self):
        SectionResult.objects.filter(session=self.session, center_id=2, report="perf").update(status="failed")
        SectionResult.objects.filter(session=self.session, center_id=3, report="gym").update(status="slow")
        SectionResult.objects.filter(session=self.session, center_id=4, report="sales").update(status="empty", row_count=0)
        grid = selectors.monitor(self.session)
        reports = grid["reports"]
        color = {r["center_id"]: [c["color"] for c in r["cells"]] for r in grid["rows"]}
        self.assertEqual(color[2][reports.index("perf")], "bg-red-500")
        self.assertEqual(color[3][reports.index("gym")], "bg-orange-500")
        self.assertEqual(color[4][reports.index("sales")], "bg-blue-500")
        self.assertEqual(color[1][reports.index("sales")], "bg-green-500")
        counts = {item["label"]: item["count"] for item in grid["legend"]}
        self.assertEqual((counts["failed"], counts["slow (timed out)"], counts["empty"]), (1, 1, 1))
        self.assertFalse(grid["live"])

    def test_monitor_marks_in_flight_cells_of_the_running_job(self):
        SectionResult.objects.filter(session=self.session, center_id=1, report="sales").delete()
        job = Job.objects.create(id="j1", state=Job.RUNNING, session=self.session, heartbeat=timezone.now(),
                                 active=["1|sales"])
        grid = selectors.monitor(self.session, job)
        cell = grid["rows"][0]["cells"][grid["reports"].index("sales")]
        self.assertIn("animate-pulse", cell["color"])
        self.assertTrue(grid["live"])
        self.assertIsNone(cell["href"])  # nothing to open yet: no result row

    def test_analytics_totals(self):
        a = selectors.analytics(self.session)
        t = a["totals"]
        self.assertEqual((t["centers"], t["sales_count"], t["sales_revenue"], t["total_revenue"]), (4, 10, 100, 400))
        self.assertEqual(t["active_rate"], 100.0)
        self.assertEqual(a["centers"][2]["sales"], 3)
        self.assertEqual(a["packs"][0]["sales"], 10)

    def test_compare_finds_differences_and_one_sided_centers(self):
        other = engine.run_session(engine.new_session("2026-08-01", "2026-08-31", [1, 2], ["sales", "perf"]))
        rows = {r["id"]: r for r in selectors.compare(self.session, other)}
        self.assertEqual(rows[1]["diff"]["sales"], 0)
        self.assertNotIn("b", rows[3])
        self.assertTrue(rows[3]["one_sided"] and not rows[1]["one_sided"])
        # only in A: its sales count as dropped, so a page summing the diffs includes it (G30)
        self.assertEqual(rows[3]["diff"]["sales"], -rows[3]["a"]["sales"])
        self.assertEqual(sum(r["diff"]["sales"] for r in rows.values()),
                         selectors.compare_totals(list(rows.values()))["sales"]["change"])

    def test_compare_totals_include_one_sided_gyms_and_say_when_rows_were_pruned(self):
        other = engine.run_session(engine.new_session("2026-08-01", "2026-08-31", [1, 2], ["sales", "perf"]))
        rows = selectors.compare(self.session, other)
        totals = selectors.compare_totals(rows)
        # gyms 3 and 4 are only in the first session: their sales count as dropped
        self.assertEqual((totals["sales"]["a"], totals["sales"]["b"], totals["sales"]["change"]), (10, 3, -7))
        self.assertEqual(totals["total_revenue"]["change"], totals["total_revenue"]["b"] - totals["total_revenue"]["a"])
        self.assertEqual(selectors.compare_notices(self.session, other), [])
        Session.objects.filter(pk=self.session.pk).update(rows_pruned=True)
        self.session.refresh_from_db()
        self.assertIn(self.session.session_id, selectors.compare_notices(self.session, other)[0])
        self.assertTrue(selectors.analytics(self.session)["rows_pruned"])

    def test_home_alerts(self):
        with mock.patch.object(auth, "state", lambda fresh=False: auth.REJECTED):
            home = selectors.home()
        texts = " ".join(a["text"] for a in home["alerts"])
        self.assertIn("Not logged in", texts)
        self.assertEqual(home["kpis"]["centers"], 4)
        self.assertEqual(home["latest"], self.session)
        Session.objects.filter(pk=self.session.pk).update(status="incomplete", missing={"perf": ["1"]})
        with mock.patch.object(auth, "state", lambda fresh=False: auth.OK):
            texts = " ".join(a["text"] for a in selectors.home()["alerts"])
        self.assertIn("unfinished session", texts)
        self.assertNotIn("Not logged in", texts)
        with mock.patch.object(auth, "state", lambda fresh=False: auth.UNREACHABLE):
            texts = " ".join(a["text"] for a in selectors.home()["alerts"])
        self.assertIn("Cannot reach Kronos", texts)
        self.assertNotIn("Not logged in", texts)

    def test_home_survives_sparse_coverage_and_lists_recent_failures(self):
        from django_q.models import Failure
        from scraper import checks
        Session.objects.filter(pk=self.session.pk).update(
            coverage=[{"report": "sales"}, {"report": "perf", "failed": 2}],  # rows written by older code
            checks=[checks.post_processing_error("boom")])
        Job.objects.create(id="old", state=Job.ERROR, error="disk full")
        Job.objects.create(id="new", state=Job.DONE)
        Failure.objects.create(id="t1", name="scrape-x", func="scraper.tasks.run", started=timezone.now(),
                               stopped=timezone.now(), success=False, result="Traceback")
        with mock.patch.object(auth, "state", lambda fresh=False: auth.OK):
            alerts = selectors.home()["alerts"]
        texts = " ".join(a["text"] for a in alerts)
        self.assertIn("2 failed center/section pairs", texts)
        self.assertIn("1 other job(s) ended in error", texts)
        self.assertIn("1 job-runner task(s) failed", texts)
        self.assertEqual(texts.count("Post-processing failed"), 1)  # listed once, not twice
        # an older session whose after-run step failed (the engine's own finding: step + level 'error')
        older = engine.new_session("2026-06-01", "2026-06-30", [1], ["perf"])
        Session.objects.filter(pk=older.pk).update(
            created_at=timezone.now() - timedelta(days=1), coverage=None,
            checks=[{"level": "error", "step": "Monthly performance table", "text": "Monthly performance table failed"},
                    {"level": "warning", "text": "3 ACTIVE center(s) returned no sales rows"}])
        with mock.patch.object(auth, "state", lambda fresh=False: auth.OK):
            alerts = selectors.home()["alerts"]
        step = [a for a in alerts if "Monthly performance table failed" in a["text"]]
        self.assertEqual([a["level"] for a in step], ["danger"])
        self.assertNotIn("returned no sales rows", " ".join(a["text"] for a in alerts))  # plain findings stay on their session

    def test_queryset_helpers(self):
        self.assertEqual(Session.objects.for_period("2026-07-01", "2026-07-31").first(), self.session)
        self.assertEqual(Session.objects.unfinished().count(), 0)
        Session.objects.filter(pk=self.session.pk).update(status="interrupted")
        self.assertEqual(engine.find_unfinished("2026-07-01", "2026-07-31"), self.session)
        job = Job.objects.create(id="x", state=Job.RUNNING, total=10, done=5,
                                 started_at=timezone.now() - timedelta(seconds=100))
        self.assertEqual(job.percent, 50)
        self.assertAlmostEqual(job.eta_seconds, 100, delta=2)
        self.assertEqual(list(Job.objects.active()), [job])

    def test_sales_window_notice_only_for_old_periods_that_include_sales(self):
        today = timezone.localdate()
        old, recent = today - timedelta(days=120), today - timedelta(days=10)
        self.assertIn("last 90 days", selectors.sales_window_notice(old, ["sales", "perf"]))
        self.assertIsNone(selectors.sales_window_notice(old, ["perf", "gym"]))
        self.assertIsNone(selectors.sales_window_notice(recent, ["sales"]))
        self.assertIsNone(selectors.sales_window_notice(None, ["sales"]))


class EstimateSalesPiecesTests(TransactionTestCase):
    def test_sales_asked_per_day_multiplies_the_requests(self):
        from scraper import selectors
        one = selectors.estimate(10, ["sales", "perf"], 2)
        many = selectors.estimate(10, ["sales", "perf"], 2, sales_pieces=30)
        self.assertEqual((one["pairs"], many["pairs"]), (20, 10 * 31))
        self.assertGreater(many["seconds_low"], one["seconds_low"])
