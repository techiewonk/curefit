from unittest import mock

from django.test import Client, TransactionTestCase
from django.utils import timezone

from centers import services as center_services
from centers.models import Center, CenterSyncLog
from console import views
from console.forms import CompareForm, OtpForm, RunForm
from scraper import auth, engine, jobs
from scraper.models import Job, Session
from scraper.testing import FakeKronosMixin

BASE = "/admin/console/"


class ConsoleViewTests(FakeKronosMixin, TransactionTestCase):
    """Pages and HTMX fragments render, forms validate, actions do what they say."""

    def setUp(self):
        super().setUp()
        center_services.set_list("test", [1, 2, 3, 4])
        center_services.set_list("actual", [1, 2])
        self.session = engine.run_session(
            engine.new_session("2026-07-01", "2026-07-31", [1, 2, 3, 4], list(engine.REPORTS)))
        self.logged_in = True
        for patcher in (mock.patch.object(auth, "logged_in", lambda fresh=False: self.logged_in),
                        mock.patch.object(auth, "send_otp", lambda: True),
                        mock.patch.object(auth, "verify_otp", lambda otp: otp == "123456")):
            patcher.start()
            self.addCleanup(patcher.stop)

    # ------------------------------------------------------------------ pages
    def test_every_page_renders(self):
        for url in ("run/", "monitor/", "analytics/", "compare/", "centers/"):
            r = self.client.get(BASE + url)
            self.assertEqual(r.status_code, 200, url)
            self.assertContains(r, "htmx.js")
        self.assertContains(self.client.get("/admin/"), "Needs attention")

    def test_analytics_and_monitor_show_the_picked_session(self):
        r = self.client.get(BASE + "analytics/", {"session": self.session.pk})
        self.assertContains(r, "Per-center analysis")
        self.assertContains(r, "Cult 3")
        self.assertEqual(r.context["totals"]["sales_count"], 10)
        r = self.client.get(BASE + "monitor/", {"session": self.session.pk})
        self.assertContains(r, f"monitor/grid/?session={self.session.pk}")

    def test_monitor_grid_fragment(self):
        r = self.client.get(BASE + "monitor/grid/", {"session": self.session.pk})
        self.assertContains(r, "Centers × reports")
        self.assertContains(r, "bg-green-500")
        self.assertEqual(len(r.context["grid"]["rows"]), 4)

    def test_compare_page(self):
        other = engine.run_session(engine.new_session("2026-08-01", "2026-08-31", [1, 2], ["sales", "perf"]))
        r = self.client.get(BASE + "compare/", {"a": self.session.pk, "b": other.pk, "only_changed": "on"})
        self.assertEqual(r.status_code, 200)
        ids = [row["id"] for row in r.context["rows"]]
        self.assertEqual(ids, [3, 4])  # only the centers that are not in both sessions have changed
        r = self.client.get(BASE + "compare/", {"a": self.session.pk, "b": "nope"})
        self.assertContains(r, "Select a valid choice")

    # ------------------------------------------------------------------ run
    def test_run_form_posts_a_job(self):
        data = {"start": "2026-09-01", "end": "2026-09-30", "centers": "test", "speed": "fast",
                "reports": ["sales", "perf"]}
        with mock.patch.object(jobs, "async_task") as queued:
            r = self.client.post(BASE + "run/", data, follow=True)
        self.assertEqual(r.status_code, 200)
        queued.assert_called_once()
        job = Job.objects.get()
        self.assertEqual((job.state, job.spec["workers"], job.spec["only"]), (Job.QUEUED, 4, ["sales", "perf"]))
        self.assertContains(r, "queued for session")

    def test_run_form_errors(self):
        data = {"start": "2026-09-30", "end": "2026-09-01", "centers": "test", "speed": "normal", "reports": ["sales"]}
        r = self.client.post(BASE + "run/", data)
        self.assertContains(r, "end date is before")
        self.assertFalse(Job.objects.exists())
        self.logged_in = False
        r = self.client.post(BASE + "run/", {**data, "start": "2026-09-01", "end": "2026-09-30"})
        self.assertContains(r, "Not logged in")
        self.assertFalse(Job.objects.exists())

    def test_run_form_continues_an_unfinished_session(self):
        Session.objects.filter(pk=self.session.pk).update(status="incomplete")
        data = {"start": "2026-07-01", "end": "2026-07-31", "centers": "test", "speed": "normal",
                "reports": ["sales"], "session": self.session.pk}
        with mock.patch.object(jobs, "async_task"):
            self.client.post(BASE + "run/", data)
        self.assertEqual(Job.objects.get().session, self.session)

    def test_resume_fragment_offers_unfinished_sessions_for_the_period(self):
        Session.objects.filter(pk=self.session.pk).update(status="incomplete")
        r = self.client.get(BASE + "run/resume/", {"start": "2026-07-01", "end": "2026-07-31"})
        self.assertContains(r, self.session.pk)
        self.assertContains(r, "Start a new session")
        r = self.client.get(BASE + "run/resume/", {"start": "2026-08-01", "end": "2026-08-31"})
        self.assertContains(r, "No unfinished session")

    def test_estimate_fragment(self):
        r = self.client.get(BASE + "run/estimate/", {"centers": "test", "reports": ["sales", "perf"], "speed": "normal"})
        self.assertContains(r, "4 centers × 2 reports = 8 fetches")
        self.assertNotContains(self.client.get(BASE + "run/estimate/", {"centers": "test"}), "fetches")

    def test_login_fragment(self):
        self.assertContains(self.client.get(BASE + "run/login/"), "logged in")
        self.assertContains(self.client.post(BASE + "run/login/", {"action": "send"}), "OTP sent")
        self.assertContains(self.client.post(BASE + "run/login/", {"action": "verify", "otp": "123456"}), "Logged in.")
        self.assertContains(self.client.post(BASE + "run/login/", {"action": "verify", "otp": "000000"}), "not accepted")
        self.assertContains(self.client.post(BASE + "run/login/", {"action": "verify", "otp": "abc"}), "numeric code")

    def test_job_fragment_pause_and_stop(self):
        self.assertContains(self.client.get(BASE + "run/job/"), "Current run")
        job = Job.objects.create(id="j", state=Job.RUNNING, session=self.session, heartbeat=timezone.now(), total=4)
        self.assertContains(self.client.get(BASE + "run/job/"), "Pause")
        self.client.post(BASE + "run/job/pause/")
        job.refresh_from_db()
        self.assertTrue(job.pause_requested)
        self.assertContains(self.client.get(BASE + "run/job/"), "Resume")
        self.client.post(BASE + "run/job/pause/")
        self.client.post(BASE + "run/job/stop/")
        job.refresh_from_db()
        self.assertEqual((job.pause_requested, job.stop_requested), (False, True))

    # ------------------------------------------------------------------ centers
    def test_centers_sync(self):
        diff = {"added": [], "removed": [], "state_changed": [], "renamed": [], "total": 4, "active": 4}
        with mock.patch.object(views.sync, "sync_from_kronos", lambda: diff):
            r = self.client.post(BASE + "centers/", follow=True)
        self.assertContains(r, "Updated 4 centers")
        self.logged_in = False
        self.assertContains(self.client.post(BASE + "centers/", follow=True), "Not logged in")

    # ------------------------------------------------------------------ forms
    def test_forms(self):
        self.assertFalse(OtpForm({"otp": "12"}).is_valid())
        self.assertTrue(OtpForm({"otp": "123456"}).is_valid())
        form = RunForm({"start": "2026-07-01", "end": "2026-07-31", "centers": "actual", "speed": "gentle",
                        "reports": list(engine.REPORTS)})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.to_spec()["only"])  # every report = no filter
        self.assertEqual(form.workers, 1)
        self.assertFalse(RunForm({"start": "x", "end": "y", "centers": "nope", "speed": "warp", "reports": []}).is_valid())
        self.assertEqual(CompareForm().fields["a"].queryset.count(), 1)

    # ------------------------------------------------------------------ safety
    def test_post_needs_csrf_and_host_is_checked(self):
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(strict.post(BASE + "run/job/stop/").status_code, 403)
        quiet = Client(raise_request_exception=False)
        self.assertEqual(quiet.get(BASE + "run/", HTTP_HOST="evil.com").status_code, 400)

    def test_sync_logs_changes(self):
        center_services.upsert_from_kronos([{"id": 1, "name": "Renamed", "state": "INACTIVE"},
                                            {"id": 99, "name": "New", "state": "ACTIVE"}])
        log = CenterSyncLog.objects.get()
        self.assertEqual((len(log.added), len(log.renamed), len(log.state_changed)), (1, 1, 1))
        self.assertEqual(Center.objects.get(pk=1).name, "Renamed")
