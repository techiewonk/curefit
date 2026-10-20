"""Web layer: every console page and download, the safety checks, and the forms' edge cases."""
import datetime as dt
import logging
import sys
from unittest import mock

import requests
from django.conf import settings
from django.test import Client, SimpleTestCase, TransactionTestCase
from django.urls import reverse

from centers import services as center_services
from centers.models import Center
from config.logscrub import ScrubToken
from console import charts, forms as console_forms, views
from console.forms import RunForm
from console.templatetags.formats import inr
from scraper import auth, engine, selectors
from scraper.models import AutoRun, GymMember, Job, SalesRecord, Session
from scraper.testing import FakeKronosMixin

BASE = "/admin/console/"


class WebPageTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        center_services.set_list("test", [1, 2, 3, 4])
        center_services.set_list("actual", [1, 2])
        self.session = engine.run_session(
            engine.new_session("2026-07-01", "2026-07-31", [1, 2, 3, 4], list(engine.REPORTS)))
        self.login = mock.patch.object(auth, "logged_in", lambda fresh=False: True)
        self.login.start()
        self.addCleanup(self.login.stop)

    # ------------------------------------------------------------------ every page and download
    def test_every_console_url_renders_with_data(self):
        pages = ["status/", "run/", "monitor/", "analytics/", "compare/", "coverage/", "perf/analysis/",
                 "gym/1/", "revenue/", "centers/", f"analytics/?session={self.session.pk}",
                 f"compare/?a={self.session.pk}&b={self.session.pk}", "run/job/", "run/login/", "monitor/grid/",
                 "run/estimate/?centers=test&reports=sales&speed=normal", "run/resume/?start=2026-07-01&end=2026-07-31"]
        for url in pages:
            self.assertEqual(self.client.get(BASE + url).status_code, 200, url)
        downloads = ["revenue/csv/", "revenue/csv/?part=groups", f"analytics/csv/?session={self.session.pk}",
                     f"compare/csv/?a={self.session.pk}&b={self.session.pk}"]
        for url in downloads:
            for fmt in ("", "&format=xlsx" if "?" in url else "?format=xlsx"):
                r = self.client.get(BASE + url + fmt)
                self.assertEqual(r.status_code, 200, url + fmt)
                self.assertIn("attachment", r["Content-Disposition"])
        self.assertEqual(self.client.get(BASE + "gym/999/").status_code, 404)
        self.assertContains(self.client.get(BASE + "status/"), "logs/curefit.log")
        self.assertEqual(self.client.get(BASE + "perf/").status_code, 302)

    def test_downloads_neutralise_formulas_and_bad_characters(self):
        Center.objects.filter(pk=1).update(name="=1+1")
        Center.objects.filter(pk=2).update(name="@x\x01")
        r = self.client.get(BASE + f"analytics/csv/?session={self.session.pk}")
        text = r.content.decode()
        self.assertIn("'=1+1", text)
        self.assertIn("'@x", text)
        self.assertNotIn("\x01", text)
        r = self.client.get(BASE + f"analytics/csv/?session={self.session.pk}&format=xlsx")
        self.assertEqual(r.status_code, 200)  # the control character no longer breaks the workbook
        r = self.client.get(BASE + f"compare/csv/?a={self.session.pk}&b={self.session.pk}")
        self.assertIn("'=1+1", r.content.decode())

    # ------------------------------------------------------------------ coverage
    def test_coverage_run_valid_and_invalid(self):
        r = self.client.post(BASE + "coverage/run/", {"run": "nonsense"})
        self.assertRedirects(r, BASE + "coverage/", fetch_redirect_response=False)
        with mock.patch.object(views.coverage, "fetch_missing", return_value=Job(id="j", session=self.session)) as f:
            r = self.client.post(BASE + "coverage/run/", {"run": "day:2026-07-05", "list": "test"})
        self.assertRedirects(r, BASE + "run/", fetch_redirect_response=False)
        self.assertEqual(f.call_args.kwargs, {"day": dt.date(2026, 7, 5)})
        r = self.client.post(BASE + "coverage/run/", {"run": "day:not-a-date", "list": "a b&c"})
        self.assertEqual(r["Location"], BASE + "coverage/?list=a+b%26c")  # the list name is url-encoded

    # ------------------------------------------------------------------ campaign
    def test_save_as_campaign_with_fixed_dates(self):
        data = {"start": "2026-09-01", "end": "2026-09-30", "centers": "test", "speed": "normal", "reports": ["sales"],
                "save_campaign": "1", "c-name": "September", "c-frequency": "daily", "c-run_at": "06:30",
                "c-weekday": "0", "c-month_day": "1", "c-period_rule": AutoRun.FIXED, "c-days": "1"}
        r = self.client.post(BASE + "run/", data)
        self.assertEqual(r.status_code, 302, r.context and (r.context["form"].errors, r.context["campaign"].errors))
        auto = AutoRun.objects.get(name="September")
        self.assertRedirects(r, reverse("admin:scraper_autorun_change", args=[auto.pk]), fetch_redirect_response=False)
        self.assertEqual((auto.start_date, auto.end_date), (dt.date(2026, 9, 1), dt.date(2026, 9, 30)))
        # a broken Run form with 'Fixed dates' shows the errors instead of a server error
        r = self.client.post(BASE + "run/", {**data, "c-name": "Other", "start": "", "end": ""})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(AutoRun.objects.filter(name="Other").exists())

    # ------------------------------------------------------------------ run page
    def test_run_page_shows_every_option_and_session_errors(self):
        r = self.client.get(BASE + "run/")
        self.assertContains(r, 'name="retry_empty"')
        self.assertContains(r, 'name="recheck_known"')
        self.assertContains(r, "known empty for a report are normally skipped")
        data = {"start": "2026-09-01", "end": "2026-09-30", "centers": "test", "speed": "normal",
                "reports": ["sales"], "session": "no-such-session"}
        self.assertContains(self.client.post(BASE + "run/", data), "Select a valid choice")

    def test_resume_panel_no_longer_duplicates_retry_empty(self):
        Session.objects.filter(pk=self.session.pk).update(status="incomplete")
        r = self.client.get(BASE + "run/resume/", {"start": "2026-07-01", "end": "2026-07-31"})
        self.assertNotContains(r, 'name="retry_empty"')

    def test_run_form_caps_the_period(self):
        base = {"centers": "test", "speed": "normal", "reports": ["sales"]}
        self.assertFalse(RunForm({**base, "start": "2020-01-01", "end": "2026-09-30"}).is_valid())
        far = (dt.date.today() + dt.timedelta(days=400)).isoformat()
        form = RunForm({**base, "start": "2026-09-01", "end": far})
        self.assertFalse(form.is_valid())
        self.assertIn("future", str(form.errors["end"]))
        form = RunForm({**base, "start": "2026-01-01", "end": "2026-03-31", "sales_chunk": "week"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.piece_count(), 13)  # 90 days, a week at a time
        form = RunForm({**base, "start": "2026-01-01", "end": "2026-03-31", "sales_chunk": "day"})
        form.is_valid()
        self.assertEqual(form.piece_count(), 90)
        r = self.client.get(BASE + "run/estimate/", {**base, "start": "2026-01-01", "end": "2026-01-10",
                                                     "sales_chunk": "day"})
        self.assertContains(r, "asked 10 times")

    def test_last_month_follows_india_time(self):
        with mock.patch.object(console_forms.timezone, "localdate", return_value=dt.date(2026, 3, 1)):
            self.assertEqual(console_forms.last_month(), (dt.date(2026, 2, 1), dt.date(2026, 2, 28)))

    # ------------------------------------------------------------------ login panel
    def test_send_otp_is_not_blocked_by_the_empty_code_box(self):
        r = self.client.get(BASE + "run/login/")
        self.assertRegex(r.content.decode(), r'value="send"[^>]*formnovalidate')

    def test_login_panel_survives_a_network_error(self):
        with mock.patch.object(auth, "send_otp", side_effect=requests.ConnectionError("down")):
            r = self.client.post(BASE + "run/login/", {"action": "send"})
        self.assertContains(r, "Could not reach Kronos")
        with mock.patch.object(auth, "verify_otp", side_effect=requests.Timeout("slow")):
            r = self.client.post(BASE + "run/login/", {"action": "verify", "otp": "123456"})
        self.assertContains(r, "Could not reach Kronos")

    def test_run_and_centers_say_when_kronos_cannot_be_reached(self):
        self.login.stop()
        with mock.patch.object(auth, "logged_in", side_effect=requests.ConnectionError("down")):
            data = {"start": "2026-09-01", "end": "2026-09-30", "centers": "test", "speed": "normal",
                    "reports": ["sales"]}
            self.assertContains(self.client.post(BASE + "run/", data), "Could not reach Kronos")
            self.assertContains(self.client.post(BASE + "centers/", follow=True), "Could not reach Kronos")
        with mock.patch.object(auth, "logged_in", return_value=False), \
                mock.patch.object(auth, "state", return_value=auth.UNREACHABLE):
            self.assertContains(self.client.post(BASE + "run/", data), "Could not reach Kronos")
            self.assertContains(self.client.get(BASE + "run/login/"), "Kronos unreachable")
        with mock.patch.object(auth, "logged_in", return_value=False), \
                mock.patch.object(auth, "state", return_value=auth.REJECTED):
            self.assertContains(self.client.post(BASE + "run/", data), "Not logged in")
        self.login.start()
        leak = RuntimeError("500 for https://metabase.curefit.co/api/embed/dashboard/SECRETTOKEN/x")
        with mock.patch.object(views.sync, "sync_from_kronos", side_effect=leak):
            r = self.client.post(BASE + "centers/", follow=True)
        self.assertContains(r, "Sync failed")
        self.assertNotContains(r, "SECRETTOKEN")

    # ------------------------------------------------------------------ compare
    def test_compare_formats_money_and_the_csv_follows_only_changed(self):
        other = engine.run_session(engine.new_session("2026-08-01", "2026-08-31", [1, 2], ["sales", "perf"]))
        noisy = [{"id": 1, "name": "Cult 1", "a": {"sales": 1, "sales_revenue": 354012.54000000004},
                  "b": {"sales": 1, "sales_revenue": 354012.54000000004}, "diff": {"sales": 0, "sales_revenue": 0.0}}]
        with mock.patch.object(selectors, "compare", return_value=noisy):
            r = self.client.get(BASE + "compare/", {"a": self.session.pk, "b": other.pk})
        self.assertContains(r, "3,54,013")
        self.assertNotContains(r, "54000000004")
        q = {"a": self.session.pk, "b": other.pk}
        changed = self.client.get(BASE + "compare/csv/", {**q, "only_changed": "on"}).content.decode().splitlines()
        every = self.client.get(BASE + "compare/csv/", q).content.decode().splitlines()
        self.assertEqual(len(changed) - 1, 2)  # centers 3 and 4 differ
        self.assertEqual(len(every) - 1, 4)
        self.assertEqual(Client(raise_request_exception=False).post(BASE + "compare/").status_code, 405)

    # ------------------------------------------------------------------ notices
    def test_pages_say_when_run_rows_were_removed(self):
        self.assertNotContains(self.client.get(BASE + "analytics/", {"session": self.session.pk}), "removed to save space")
        Session.objects.filter(pk=self.session.pk).update(merged_into_masters=True)
        SalesRecord.objects.filter(session=self.session).delete()
        GymMember.objects.filter(session=self.session).delete()
        self.assertContains(self.client.get(BASE + "analytics/", {"session": self.session.pk}), "removed to save space")
        self.assertContains(self.client.get(BASE + "compare/", {"a": self.session.pk, "b": self.session.pk}),
                            "removed to save space")

    def test_revenue_page_names_the_real_report_and_explains_partial_months(self):
        r = self.client.get(BASE + "revenue/")
        html = r.content.decode()
        self.assertTrue(r.context["data"]["has_data"])
        self.assertIn("days so far", html)
        self.assertIn("each counted once", html)
        with mock.patch("scraper.revenue.matrix", return_value={**r.context["data"], "has_data": False}):
            html = self.client.get(BASE + "revenue/").content.decode()
        self.assertNotIn("Franchise Performance", html)
        self.assertIn("Total Revenue and Split", html)

    def test_perf_analysis_says_how_each_metric_adds_up(self):
        r = self.client.get(BASE + "perf/analysis/")
        self.assertTrue(r.context["cards"])
        self.assertContains(r, "values are added up")


class SafetyTests(TransactionTestCase):
    """Requests another website makes the browser send are refused before the auto-login."""

    def test_cross_site_requests_are_refused(self):
        c = Client()
        self.assertEqual(c.get(BASE + "run/", HTTP_SEC_FETCH_SITE="cross-site").status_code, 403)
        self.assertEqual(c.get(BASE + "run/", HTTP_SEC_FETCH_SITE="same-site").status_code, 403)
        self.assertEqual(c.get(BASE + "run/", HTTP_ORIGIN="https://evil.example").status_code, 403)
        self.assertEqual(c.get(BASE + "run/", HTTP_REFERER="https://evil.example/page").status_code, 403)
        self.assertEqual(c.get(BASE + "run/", HTTP_ORIGIN="null").status_code, 403)
        self.assertEqual(c.get("/admin/scraper/autorun/1/run-now/", HTTP_SEC_FETCH_SITE="cross-site").status_code, 403)

    def test_own_pages_and_typed_urls_are_served(self):
        c = Client()
        self.assertEqual(c.get(BASE + "run/", HTTP_SEC_FETCH_SITE="none").status_code, 200)
        self.assertEqual(c.get(BASE + "run/", HTTP_SEC_FETCH_SITE="same-origin",
                               HTTP_REFERER="http://localhost:8000/admin/").status_code, 200)
        self.assertEqual(c.get(BASE + "run/", HTTP_ORIGIN="http://127.0.0.1:8000").status_code, 200)
        self.assertEqual(c.get(BASE + "run/").status_code, 200)  # no headers at all (scripts, tests)

    def test_own_cookie_names(self):
        self.assertEqual((settings.SESSION_COOKIE_NAME, settings.CSRF_COOKIE_NAME),
                         ("curefit_sessionid", "curefit_csrftoken"))
        r = Client().get(BASE + "run/")
        self.assertIn("curefit_sessionid", r.cookies)
        self.assertEqual(r.cookies["curefit_sessionid"]["samesite"], "Strict")

    def test_two_job_workers(self):
        self.assertEqual(settings.Q_CLUSTER["workers"], 2)


class FormatTests(SimpleTestCase):
    def test_inr_never_breaks_a_page(self):
        self.assertEqual(inr(1875475), "18,75,475")
        self.assertEqual(inr(-1234.4), "-1,234")
        self.assertEqual(inr(2.5), "2.5")
        self.assertEqual(inr(None), "")
        self.assertEqual(inr(float("nan")), "")
        self.assertEqual(inr(float("inf")), "")
        self.assertEqual(inr("12%"), "12%")
        self.assertEqual(inr("1000"), "1,000")

    def test_chart_json_is_strict(self):
        text = charts.dumps({"data": [1.5, float("nan"), None, float("inf")]})
        self.assertEqual(text, '{"data": [1.5, null, null, null]}')
        self.assertEqual(views._rounded(float("nan")), None)
        self.assertEqual(views._rounded(2.345), 2.35)

    def test_log_lines_never_carry_the_embed_token(self):
        try:
            raise RuntimeError("500 for https://metabase.curefit.co/api/embed/dashboard/SECRETTOKEN/dashcard/1")
        except RuntimeError:
            record = logging.LogRecord("scraper", logging.ERROR, __file__, 1, "fetch %s failed",
                                       ("https://metabase.curefit.co/api/embed/dashboard/SECRETTOKEN",), sys.exc_info())
        self.assertTrue(ScrubToken().filter(record))
        text = logging.Formatter().format(record)
        self.assertNotIn("SECRETTOKEN", text)
        self.assertIn("RuntimeError", text)

    def test_datatable_sorts_strictly_and_works_from_the_keyboard(self):
        import re
        from pathlib import Path
        js = (Path(settings.BASE_DIR) / "console/static/console/datatable.js").read_text(encoding="utf-8")
        pattern = re.search(r"var NUMBER = /(.+)/;", js).group(1)
        number = re.compile(pattern)
        bare = lambda t: re.sub(r"[₹%,\s]", "", t)  # noqa: E731 - the same stripping as the script
        self.assertIsNone(number.match(bare("99 Fitness")))  # a gym name is text, not 99
        for text in ("18,75,475", "12.5%", "-3", "₹1,000"):
            self.assertIsNotNone(number.match(bare(text)), text)
        for needed in ('th.tabIndex = 0', '"keydown"', 'prev.type = next.type = "button"', "heads.indexOf(th) === i"):
            self.assertIn(needed, js)

    def test_logs_also_go_to_a_rotating_file(self):
        handler = settings.LOGGING["handlers"]["file"]
        self.assertEqual(handler["class"], "logging.handlers.RotatingFileHandler")
        self.assertEqual((handler["maxBytes"], handler["backupCount"]), (5 * 1024 * 1024, 5))
        self.assertIn("scrub", handler["filters"])
        for name in ("scraper", "django-q", "django.request"):
            self.assertIn("file", settings.LOGGING["loggers"][name]["handlers"])
