import os
from unittest import mock

from constance.admin import get_values
from django.apps import apps
from django.contrib import admin
from django.test import Client, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django_q.models import Schedule

from centers import services
from centers.models import Center, CenterList, KnownEmpty
from config.admin_base import SettingsForm
from scraper import autorun, coverage, engine, jobs
from scraper.models import (AutoRun, CenterMonth, GymMember, GymPack, Job, SalesCoverage, SalesPack, SalesRecord,
                            SectionResult, Session)
from scraper.resources import SalesRecordResource, safe_cell
from scraper.testing import FakeKronosMixin


class AdminTests(FakeKronosMixin, TransactionTestCase):
    """Every admin page loads on real-shaped data; the actions do what their labels say."""

    def setUp(self):
        super().setUp()
        s = engine.new_session("2026-07-01", "2026-07-31", [1, 2, 3, 4], list(engine.REPORTS))
        self.session = engine.run_session(s)

    def url(self, model, name="changelist", *args):
        return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_{name}", args=args)

    def test_every_registered_page_loads(self):
        bad = []
        for model, model_admin in admin.site._registry.items():
            if model._meta.app_label not in ("centers", "scraper"):
                continue
            for suffix in ("", "?q=Cult", "?o=1"):
                r = self.client.get(self.url(model) + suffix)
                if r.status_code != 200:
                    bad.append((model.__name__, suffix, r.status_code))
        self.assertEqual(bad, [])

    def test_detail_pages_load(self):
        for obj in (self.session, Center.objects.first(), SectionResult.objects.first()):
            r = self.client.get(self.url(type(obj), "change", obj.pk))
            self.assertEqual(r.status_code, 200, type(obj).__name__)

    def test_filters_work(self):
        r = self.client.get(self.url(SectionResult) + "?status__exact=ok&report=sales")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["cl"].result_count, 4)
        r = self.client.get(self.url(Session) + "?status__exact=complete")
        self.assertEqual(r.context["cl"].result_count, 1)

    def test_export_pages_load(self):
        for name in ("salesrecord", "gymmember"):
            r = self.client.get(reverse(f"admin:scraper_{name}_export"))
            self.assertEqual(r.status_code, 200, name)

    def test_center_list_actions(self):
        ids = list(Center.objects.values_list("pk", flat=True))
        r = self.client.post(self.url(Center), {"action": "add_to_test", "_selected_action": ids[:2]}, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sorted(services.list_ids("test")), ids[:2])
        self.client.post(self.url(Center), {"action": "remove_from_test", "_selected_action": ids[:1]}, follow=True)
        self.assertEqual(services.list_ids("test"), ids[1:2])

    def test_session_merge_and_continue_actions(self):
        merged = os.path.join(self.session.folder, "merged", "sales.csv")
        os.remove(merged)
        self.client.post(self.url(Session), {"action": "merge_files", "_selected_action": [self.session.pk]}, follow=True)
        self.assertTrue(os.path.exists(merged))
        self.client.post(self.url(Session), {"action": "continue_session", "_selected_action": [self.session.pk]}, follow=True)
        self.assertEqual(Job.objects.filter(session=self.session, state=Job.QUEUED).count(), 1)

    def test_deleting_a_session_removes_its_folder_and_rows(self):
        folder = self.session.folder
        self.assertTrue(os.path.isdir(folder))
        r = self.client.post(self.url(Session, "delete", self.session.pk), {"post": "yes"}, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Session.objects.exists())
        self.assertFalse(os.path.isdir(folder))
        self.assertEqual(SectionResult.objects.count(), 0)

    def test_mark_known_empty_and_retry_actions(self):
        pk = SectionResult.objects.filter(report="sales", center_id=1).first().pk
        SectionResult.objects.filter(pk=pk).update(status=SectionResult.EMPTY)
        ok = SectionResult.objects.get(report="sales", center_id=2)  # status ok: must not become known empty
        r = self.client.post(self.url(SectionResult), {"action": "mark_known_empty", "_selected_action": [pk, ok.pk]},
                             follow=True)
        self.assertTrue(KnownEmpty.objects.filter(center_id=1, report="sales").exists())
        self.assertFalse(KnownEmpty.objects.filter(center_id=2).exists())
        self.assertContains(r, "1 selected result not marked")
        # retry asks first, and only the confirmed post deletes
        r = self.client.post(self.url(SectionResult), {"action": "retry_selected", "_selected_action": [pk]})
        self.assertContains(r, "Yes, go ahead")
        self.assertTrue(SectionResult.objects.filter(pk=pk).exists())
        self.client.post(self.url(SectionResult), {"action": "retry_selected", "_selected_action": [pk],
                                                   "confirmed": "yes", "index": 0}, follow=True)
        self.assertFalse(SectionResult.objects.filter(pk=pk).exists())  # cleared so the continue refetches it
        self.assertEqual(Job.objects.filter(state=Job.QUEUED).count(), 1)

    def test_retry_keeps_the_results_while_a_scrape_runs(self):
        Job.objects.create(id="busy", state=Job.RUNNING, session=self.session, heartbeat=timezone.now())
        pk = SectionResult.objects.filter(report="sales", center_id=1).first().pk
        r = self.client.post(self.url(SectionResult), {"action": "retry_selected", "_selected_action": [pk],
                                                       "confirmed": "yes", "index": 0}, follow=True)
        self.assertContains(r, "Nothing was deleted")
        self.assertTrue(SectionResult.objects.filter(pk=pk).exists())

    def test_retry_puts_the_results_back_when_the_job_cannot_be_queued(self):
        pk = SectionResult.objects.filter(report="sales", center_id=1).first().pk
        with mock.patch.object(jobs, "enqueue", side_effect=jobs.JobError("refused")):
            self.client.post(self.url(SectionResult), {"action": "retry_selected", "_selected_action": [pk],
                                                       "confirmed": "yes", "index": 0}, follow=True)
        self.assertTrue(SectionResult.objects.filter(pk=pk).exists())

    def test_scraped_and_merged_rows_cannot_be_deleted_by_hand(self):
        for model in (GymPack, SalesPack, SalesCoverage, CenterMonth, SalesRecord, GymMember):
            r = self.client.get(self.url(model))
            self.assertEqual(r.status_code, 200, model.__name__)
            self.assertFalse(r.context["cl"].model_admin.has_delete_permission(r.wsgi_request), model.__name__)
        row = GymMember.objects.first()
        self.assertEqual(self.client.get(self.url(GymMember, "delete", row.pk)).status_code, 403)
        # deleting the session still removes its rows: test_deleting_a_session_removes_its_folder_and_rows

    def test_bulk_deleting_campaigns_removes_their_schedules(self):
        auto = AutoRun.objects.create(name="t", enabled=True)
        self.assertTrue(Schedule.objects.filter(name=f"autorun-{auto.pk}").exists())
        self.client.post(self.url(AutoRun), {"action": "delete_selected", "_selected_action": [auto.pk], "post": "yes"},
                         follow=True)
        self.assertFalse(AutoRun.objects.exists())
        self.assertFalse(Schedule.objects.filter(name=f"autorun-{auto.pk}").exists())

    def test_active_jobs_cannot_be_deleted(self):
        running = Job.objects.create(id="run", state=Job.RUNNING, session=self.session, heartbeat=timezone.now())
        Job.objects.create(id="old", state=Job.DONE, session=self.session)
        self.assertEqual(self.client.post(self.url(Job, "delete", running.pk), {"post": "yes"}).status_code, 403)
        self.client.post(self.url(Job), {"action": "delete_selected", "_selected_action": ["run", "old"],
                                         "post": "yes"}, follow=True)
        self.assertEqual(Job.objects.count(), 2)  # refused as a whole while one of them is active
        self.client.post(self.url(Job), {"action": "delete_selected", "_selected_action": ["old"], "post": "yes"})
        self.assertEqual(list(Job.objects.values_list("pk", flat=True)), ["run"])

    def test_force_stop_asks_first(self):
        job = Job.objects.create(id="j", state=Job.QUEUED, session=self.session)
        with mock.patch.object(jobs, "force_stop") as stop:
            r = self.client.post(self.url(Job), {"action": "force_stop", "_selected_action": [job.pk]})
            self.assertContains(r, "request in progress is lost")
            stop.assert_not_called()
            self.client.post(self.url(Job), {"action": "force_stop", "_selected_action": [job.pk],
                                             "confirmed": "yes", "index": 0})
            stop.assert_called_once()

    def test_run_now_only_starts_on_a_post_with_csrf(self):
        auto = AutoRun.objects.create(name="t")
        url = reverse("admin:scraper_autorun_run_now", args=[auto.pk])
        with mock.patch.object(autorun, "run", return_value=("started", "ok")) as run:
            r = self.client.get(url)  # what an <img src> on another site would do: it only shows the dialog
            self.assertContains(r, "Start the scrape")
            run.assert_not_called()
            strict = Client(enforce_csrf_checks=True)
            self.assertEqual(strict.post(url, {"_form_submitted": "True"}).status_code, 403)
            run.assert_not_called()
            r = self.client.post(url, {"_form_submitted": "True"}, HTTP_REFERER="http://localhost:8000/elsewhere/")
            run.assert_called_once()
            self.assertRedirects(r, self.url(AutoRun), fetch_redirect_response=False)  # not to the Referer
            missing = reverse("admin:scraper_autorun_run_now", args=[999])
            self.assertEqual(self.client.post(missing, {"_form_submitted": "True"}).status_code, 404)

    def test_fetch_missing_days_only_starts_on_a_post(self):
        url = reverse("admin:scraper_salescoverage_fetch_missing_days")
        with mock.patch.object(coverage, "fetch_missing", return_value=Job(id="j", session=self.session)) as fetch:
            self.assertContains(self.client.get(url), "Start fetching")
            fetch.assert_not_called()
            r = self.client.post(url, {"_form_submitted": "True"}, HTTP_HX_REQUEST="true")
            fetch.assert_called_once_with("actual")
            self.assertEqual(r["HX-Redirect"], reverse("console:run"))

    def test_session_detail_says_when_run_rows_were_removed(self):
        self.assertContains(self.client.get(self.url(Session, "change", self.session.pk)), "Kept")
        Session.objects.filter(pk=self.session.pk).update(merged_into_masters=True)
        SalesRecord.objects.filter(session=self.session).delete()
        GymMember.objects.filter(session=self.session).delete()
        self.assertContains(self.client.get(self.url(Session, "change", self.session.pk)), "Removed to save space")

    def test_settings_refuse_unsafe_values(self):
        r = self.client.get(reverse("admin:constance_config_changelist"))
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.context["form"], SettingsForm)
        initial = get_values()
        base = {**initial, "version": SettingsForm(initial=initial).initial["version"]}
        self.assertTrue(SettingsForm(initial=initial, data=base).is_valid(), SettingsForm(initial=initial, data=base).errors)
        for name, bad in (("MAX_RETRIES", 0), ("MAX_WORKERS", 50), ("REQUEST_TIMEOUT", 0), ("DELAY_MIN", -1),
                          ("POLL_SECONDS", 5), ("AUTH_FAIL_LIMIT", 0), ("LOGIN_EMAIL", "not an email"),
                          ("SALES_CHUNK", "month")):
            self.assertFalse(SettingsForm(initial=initial, data={**base, name: bad}).is_valid(), name)
        form = SettingsForm(initial=initial, data={**base, "DELAY_MIN": 5, "DELAY_MAX": 1})
        self.assertFalse(form.is_valid())
        self.assertIn("DELAY_MIN", form.errors)

    def test_exports_neutralise_formulas(self):
        Center.objects.filter(pk=1).update(name="=1+1")
        SalesRecord.objects.filter(center_id=1).update(name="@x", phone="-5")
        SalesRecord.objects.filter(center_id=2).update(name="bad\x01name")
        data = SalesRecordResource().export(SalesRecord.objects.filter(center_id__in=[1, 2]))
        rows = [dict(zip(data.headers, row)) for row in data]
        one = next(r for r in rows if str(r["center_id"]) == "1")
        self.assertEqual((one["center_name"], one["name"], one["phone"]), ("'=1+1", "'@x", "-5"))
        self.assertIn("badname", [r["name"] for r in rows])
        data.export("xlsx")  # no illegal character left to break the workbook
        self.assertEqual([safe_cell(v) for v in ("+91 98", "-x", "12.5%", 7, None)],
                         ["'+91 98", "'-x", "12.5%", 7, None])

    def test_no_login_page_but_bad_host_is_refused(self):
        self.assertEqual(self.client.get("/admin/").status_code, 200)  # auto-login
        # a real server answers 400; the test client would otherwise re-raise the exception
        quiet = Client(raise_request_exception=False)
        self.assertEqual(quiet.get("/admin/", HTTP_HOST="evil.com").status_code, 400)

    def test_center_list_admin_uses_autocomplete(self):
        lst = services.get_list("test")
        r = self.client.get(self.url(CenterList, "change", lst.pk))
        self.assertEqual(r.status_code, 200)
        self.assertIn("autocomplete", r.content.decode().lower())
