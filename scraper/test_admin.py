import os

from django.apps import apps
from django.contrib import admin
from django.test import Client, TransactionTestCase
from django.urls import reverse

from centers import services
from centers.models import Center, CenterList, KnownEmpty
from scraper import engine
from scraper.models import Job, SectionResult, Session
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
        self.client.post(self.url(SectionResult), {"action": "mark_known_empty", "_selected_action": [pk]}, follow=True)
        self.assertTrue(KnownEmpty.objects.filter(center_id=1, report="sales").exists())
        self.client.post(self.url(SectionResult), {"action": "retry_selected", "_selected_action": [pk]}, follow=True)
        self.assertFalse(SectionResult.objects.filter(pk=pk).exists())  # cleared so the continue refetches it
        self.assertEqual(Job.objects.filter(state=Job.QUEUED).count(), 1)

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
