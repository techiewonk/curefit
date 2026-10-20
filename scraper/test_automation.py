import datetime as dt
from io import StringIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django_q.models import Schedule

from centers import services
from scraper import auth, autorun, engine, jobs, storage
from scraper.models import AutoRun, Job, SalesRecord, Session
from scraper.testing import FakeKronosMixin


class AutoRunTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        services.set_list("test", [1, 2])
        from centers.models import CenterList
        self.list = CenterList.objects.get(name="test")
        for patcher in (mock.patch.object(auth, "logged_in", lambda fresh=False: self.logged_in),):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.logged_in = True

    def auto(self, **kw):
        fields = dict(name="Daily test", frequency=AutoRun.DAILY, run_at=dt.time(6, 30), centers=self.list,
                      reports=["sales", "perf"], speed="normal", granularity=Session.DAY, enabled=False)
        fields.update(kw)
        return AutoRun.objects.create(**fields)

    # ------------------------------------------------------------------ periods
    def test_periods(self):
        today = dt.date(2026, 10, 21)  # a Wednesday
        self.assertEqual(autorun.period(self.auto(days=1), today), (dt.date(2026, 10, 20),) * 2)
        self.assertEqual(autorun.period(self.auto(name="n", days=3), today), (dt.date(2026, 10, 18), dt.date(2026, 10, 20)))
        self.assertEqual(autorun.period(self.auto(name="w", period_rule=AutoRun.PREV_WEEK), today),
                         (dt.date(2026, 10, 12), dt.date(2026, 10, 18)))
        self.assertEqual(autorun.period(self.auto(name="m", period_rule=AutoRun.PREV_MONTH), today),
                         (dt.date(2026, 9, 1), dt.date(2026, 9, 30)))
        self.assertEqual(autorun.period(self.auto(name="t", period_rule=AutoRun.MONTH_TO_DATE), today),
                         (dt.date(2026, 10, 1), dt.date(2026, 10, 20)))
        self.assertEqual(autorun.period(self.auto(name="t1", period_rule=AutoRun.MONTH_TO_DATE), dt.date(2026, 11, 1)),
                         (dt.date(2026, 10, 1), dt.date(2026, 10, 31)))  # on the 1st, "so far" is still last month
        self.assertEqual(autorun.period(self.auto(name="f", period_rule=AutoRun.FIXED, start_date=dt.date(2026, 1, 1),
                                                  end_date=dt.date(2026, 1, 31)), today), (dt.date(2026, 1, 1), dt.date(2026, 1, 31)))
        self.assertEqual(autorun.period(self.auto(name="s", period_rule=AutoRun.MISSING_SALES), today),
                         (today - dt.timedelta(days=90), dt.date(2026, 10, 20)))

    def test_next_occurrence_is_in_the_future(self):
        now = timezone.now()
        for freq in (AutoRun.DAILY, AutoRun.WEEKLY, AutoRun.MONTHLY, AutoRun.CRON):
            auto = self.auto(name=freq, frequency=freq, cron="15 7 * * *")
            self.assertGreater(autorun.next_occurrence(auto, now), now, freq)

    def test_cron_is_validated(self):
        with self.assertRaises(ValidationError):
            self.auto(name="bad", frequency=AutoRun.CRON, cron="not a cron").full_clean()

    # ------------------------------------------------------------------ schedule
    def test_schedule_follows_the_switch(self):
        auto = self.auto()
        self.assertFalse(Schedule.objects.filter(name=autorun.schedule_name(auto)).exists())
        auto.enabled = True
        auto.save()
        schedule = Schedule.objects.get(name=autorun.schedule_name(auto))
        self.assertEqual((schedule.func, schedule.schedule_type), (autorun.TASK, Schedule.DAILY))
        auto.frequency, auto.cron = AutoRun.CRON, "0 5 * * *"
        auto.save()
        self.assertEqual(Schedule.objects.get(pk=schedule.pk).schedule_type, Schedule.CRON)
        auto.enabled = False
        auto.save()
        self.assertFalse(Schedule.objects.filter(name=autorun.schedule_name(auto)).exists())
        auto.enabled = True
        auto.save()
        auto.delete()
        self.assertFalse(Schedule.objects.filter(name=autorun.schedule_name(auto)).exists())

    # ------------------------------------------------------------------ running
    def test_guards(self):
        auto = self.auto()
        self.assertEqual(autorun.run(auto)[0], "skipped")  # switched off
        self.logged_in = False
        self.assertIn("Not logged in", autorun.run(auto, force=True)[1])

    def test_inline_run_makes_a_session_for_yesterday_split_by_day(self):
        auto = self.auto(days=1)
        status, _ = autorun.run(auto, force=True, inline=True)
        self.assertIn(status, ("finished", "incomplete"))
        auto.refresh_from_db()
        yesterday = timezone.localdate() - dt.timedelta(days=1)
        session = auto.last_session
        self.assertEqual((session.start_date, session.end_date, session.granularity),
                         (yesterday, yesterday, Session.DAY))
        self.assertIn("day", {p.get("granular") for p in self.params})

    def test_a_run_is_not_started_twice_while_its_last_start_is_still_going(self):
        auto = self.auto(enabled=True)
        with mock.patch("scraper.jobs.async_task"):
            self.assertEqual(autorun.run(auto)[0], "started")
            job = Job.objects.get()
            Job.objects.filter(pk=job.pk).update(state=Job.RUNNING, heartbeat=timezone.now())
            status, message = autorun.run(auto)
            self.assertEqual(status, "skipped")
            self.assertIn("still going", message)
            auto.refresh_from_db()
            self.assertEqual((auto.last_status, auto.last_session_id), ("started", job.session_id))  # left as it was
            Job.objects.filter(pk=job.pk).update(state=Job.DONE)
            self.assertEqual(autorun.run(auto)[0], "started")  # a second run the same day is fine once it ended
        self.assertEqual(Job.objects.count(), 2)

    def test_command(self):
        self.auto(name="Nightly")
        out = StringIO()
        call_command("auto_run", "--list", stdout=out)
        self.assertIn("Nightly", out.getvalue())
        with self.assertRaises(SystemExit) as raised:
            call_command("auto_run", "Nightly", stdout=out)  # switched off, so skipped
        self.assertEqual(raised.exception.code, 2)


class StopTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        services.set_list("test", [1, 2])

    def queued(self):
        return jobs.enqueue({"start": "2026-07-01", "end": "2026-07-31", "list": "test", "workers": 1},
                            sync=False)

    def test_a_queued_job_is_cancelled(self):
        with mock.patch("scraper.jobs.async_task"):
            job = self.queued()
        jobs.request_stop(job)
        job.refresh_from_db()
        self.assertEqual(job.state, Job.CANCELLED)
        self.assertEqual(job.session.status, Session.INCOMPLETE)

    def test_a_running_job_gets_a_stop_flag_then_force_kills_the_runner(self):
        with mock.patch("scraper.jobs.async_task"):
            job = self.queued()
        Job.objects.filter(pk=job.pk).update(state=Job.RUNNING, pid=999999)
        job.refresh_from_db()
        jobs.request_stop(job)
        job.refresh_from_db()
        self.assertTrue(job.stop_requested)
        with mock.patch("scraper.jobs._kill") as kill:
            jobs.force_stop(job)
        kill.assert_called_once_with(999999)
        job.refresh_from_db()
        self.assertEqual(job.state, Job.INTERRUPTED)
        self.assertEqual(job.session.status, Session.INTERRUPTED)

    def test_job_action_redirects_only_to_local_pages(self):
        with mock.patch("scraper.jobs.async_task"):
            job = self.queued()
        r = self.client.post("/admin/console/run/job/stop/", {"next": "/admin/"})
        self.assertRedirects(r, "/admin/", fetch_redirect_response=False)
        job.refresh_from_db()
        self.assertEqual(job.state, Job.CANCELLED)
        r = self.client.post("/admin/console/run/job/stop/", {"next": "https://evil.example/"})
        self.assertEqual(r.status_code, 200)  # not followed


class DataTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.session = engine.run_session(engine.new_session("2026-07-01", "2026-07-31", [1, 2, 3, 4],
                                                             list(engine.REPORTS)))

    def test_day_parsing(self):
        self.assertEqual(storage._day("2026-06-11T00:00:00Z"), dt.date(2026, 6, 11))
        self.assertEqual(storage._day("2026-06-11"), dt.date(2026, 6, 11))
        self.assertIsNone(storage._day(""))
        self.assertIsNone(storage._day("n/a"))

    def test_range_filters_on_every_data_list(self):
        for model in ("salesrecord", "gymmember", "sectionresult"):
            url = reverse(f"admin:scraper_{model}_changelist")
            self.assertEqual(self.client.get(url).status_code, 200, model)
        url = reverse("admin:scraper_salesrecord_changelist")
        everything = self.client.get(url).context["cl"].result_count
        self.assertEqual(everything, SalesRecord.objects.count())
        self.assertEqual(self.client.get(url, {"revenue_from": "999999999"}).context["cl"].result_count, 0)
        self.assertEqual(self.client.get(url, {"revenue_from": "0", "revenue_to": "999999999"})
                         .context["cl"].result_count, everything)

    def test_export_pages_open(self):
        for model in ("salesrecord", "gymmember", "centermonth", "sectionresult", "session"):
            r = self.client.get(reverse(f"admin:scraper_{model}_export"))
            self.assertEqual(r.status_code, 200, model)

    def test_console_csv_downloads(self):
        r = self.client.get("/admin/console/analytics/csv/", {"session": self.session.pk})
        self.assertEqual(r["Content-Type"].split(";")[0], "text/csv")
        self.assertTrue(r.content.decode().startswith("center_id,center,sales"))
        r = self.client.get("/admin/console/analytics/csv/", {"session": self.session.pk, "format": "xlsx"})
        self.assertIn("spreadsheetml", r["Content-Type"])
        other = engine.run_session(engine.new_session("2026-08-01", "2026-08-31", [1, 2], ["sales"]))
        r = self.client.get("/admin/console/compare/csv/", {"a": self.session.pk, "b": other.pk})
        self.assertEqual(r.status_code, 200)
        self.assertIn("sales_change", r.content.decode().splitlines()[0])

    def test_trend_and_pages(self):
        self.assertEqual(self.client.get("/admin/console/analytics/", {"session": self.session.pk}).status_code, 200)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:scraper_autorun_changelist")).status_code, 200)


class CampaignTests(FakeKronosMixin, TransactionTestCase):
    """An automatic run is a campaign: the whole Run-page setup attached to a schedule."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        from centers.models import CenterGroup, CenterList
        services.set_list("test", [1, 2])
        self.list = CenterList.objects.get(name="test")
        self.group = CenterGroup.objects.get_or_create(name="GYMS")[0]
        self.group.centers.set([3])
        self.logged_in = True
        for patcher in (mock.patch.object(auth, "logged_in", lambda fresh=False: self.logged_in),
                        mock.patch("scraper.jobs.async_task")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def campaign(self, **kw):
        fields = dict(name="C", frequency=AutoRun.DAILY, run_at=dt.time(6, 30), centers=self.list,
                      reports=["sales", "perf"], speed="normal", granularity=Session.DAY)
        fields.update(kw)
        return AutoRun.objects.create(**fields)

    def test_gyms_come_from_the_list_the_groups_and_the_gyms_picked_by_hand(self):
        auto = self.campaign()
        self.assertEqual(autorun.center_ids(auto), [1, 2])
        auto.groups.add(self.group)
        auto.gyms.add(4)
        self.assertEqual(autorun.center_ids(auto), [1, 2, 3, 4])
        only_group = self.campaign(name="G", centers=None)
        only_group.groups.add(self.group)
        self.assertEqual(autorun.center_ids(only_group), [3])

    def test_a_run_carries_every_run_page_setting(self):
        auto = self.campaign(period_rule=AutoRun.PREV_WEEK, speed="fast", granularity=Session.WEEK,
                             sales_chunk=Session.DAY, fill_missing=True, retry_empty=True, recheck_known=True)
        auto.groups.add(self.group)
        status, _ = autorun.run(auto, force=True)
        self.assertEqual(status, "started")
        s = Job.objects.get().session
        self.assertEqual((s.center_ids, s.speed, s.granularity, s.sales_chunk, s.fill_missing),
                         ([1, 2, 3], "fast", Session.WEEK, Session.DAY, True))
        self.assertEqual((s.start_date, s.end_date), autorun.period(auto))

    def test_the_missing_sales_days_campaign_fetches_only_sales_day_by_day(self):
        auto = self.campaign(period_rule=AutoRun.MISSING_SALES, reports=["perf", "gym"])
        autorun.run(auto, force=True)
        s = Job.objects.get().session
        self.assertEqual((s.reports, s.sales_chunk, s.fill_missing), (["sales"], Session.DAY, True))

    def test_an_unfinished_run_of_the_same_period_is_continued(self):
        auto = self.campaign(period_rule=AutoRun.PREV_MONTH)
        autorun.run(auto, force=True)
        first = Job.objects.get()
        Job.objects.update(state=Job.INTERRUPTED)
        Session.objects.filter(pk=first.session_id).update(status=Session.INTERRUPTED)
        auto.refresh_from_db()
        status, message = autorun.run(auto, force=True)
        self.assertEqual(status, "started")
        self.assertIn("Continued", message)
        self.assertEqual(Job.objects.order_by("-created_at").first().session_id, first.session_id)
        self.assertEqual(Session.objects.count(), 1)  # no second session
        Job.objects.update(state=Job.DONE)
        auto.continue_unfinished = False
        auto.save()
        autorun.run(auto, force=True)
        self.assertEqual(Session.objects.count(), 2)  # switched off: a fresh one

    def test_a_campaign_without_gyms_is_refused(self):
        auto = self.campaign(centers=None)
        status, message = autorun.run(auto, force=True)
        self.assertEqual(status, "failed")
        self.assertIn("No gyms", message)

    def test_admin_form_validates_the_selection_and_fixed_dates(self):
        from scraper.admin import AutoRunForm
        base = {"name": "X", "enabled": "", "frequency": "daily", "run_at": "06:30:00", "weekday": 0, "month_day": 1,
                "cron": "", "period_rule": "last_days", "days": 1, "reports": ["sales"], "speed": "normal",
                "granularity": "day", "sales_chunk": "whole", "continue_unfinished": "on"}
        self.assertIn("centers", AutoRunForm(base).errors)  # nothing to run on
        self.assertNotIn("centers", AutoRunForm({**base, "centers": self.list.pk}).errors)
        self.assertNotIn("centers", AutoRunForm({**base, "groups": [self.group.pk]}).errors)
        fixed = AutoRunForm({**base, "centers": self.list.pk, "period_rule": "fixed",
                             "start_date": "2026-02-01", "end_date": "2026-01-01"})
        self.assertIn("end_date", fixed.errors)
        self.assertEqual(self.client.get(reverse("admin:scraper_autorun_add")).status_code, 200)
        auto = self.campaign()
        self.assertEqual(self.client.get(reverse("admin:scraper_autorun_change", args=[auto.pk])).status_code, 200)
        self.assertContains(self.client.get(reverse("admin:scraper_autorun_changelist")), auto.name)

    def test_migration_keeps_the_old_period_of_existing_runs(self):
        from importlib import import_module
        from django.apps import apps
        mig = import_module("scraper.migrations.0012_campaign")
        weekly = self.campaign(name="W", frequency=AutoRun.WEEKLY)
        monthly = self.campaign(name="M", frequency=AutoRun.MONTHLY)
        cron = self.campaign(name="K", frequency=AutoRun.CRON, cron="0 6 * * *")
        AutoRun.objects.update(period_rule="last_days")
        mig.keep_old_periods(apps, None)
        self.assertEqual([AutoRun.objects.get(pk=a.pk).period_rule for a in (weekly, monthly, cron)],
                         ["prev_week", "prev_month", "last_days"])

    def test_the_run_form_takes_groups_and_save_as_campaign_keeps_the_setup(self):
        from console.forms import RunForm
        post = {"start": "2026-07-01", "end": "2026-07-31", "centers": "test", "speed": "fast",
                "reports": ["sales", "gym"], "groups": [self.group.pk], "pick": [4], "granularity": "week",
                "sales_chunk": "day", "fill_missing": "on", "retry_empty": "on", "recheck_known": "on"}
        form = RunForm(post)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual((form.chosen_ids(), form.center_count, form.to_spec()["center_ids"]), ([3, 4], 2, [3, 4]))
        r = self.client.post(reverse("console:run"), {**post, "save_campaign": "1", "c-name": "Weekly sales",
                                                      "c-frequency": "weekly", "c-weekday": 2, "c-run_at": "07:15:00",
                                                      "c-month_day": 1, "c-period_rule": "prev_week", "c-days": 1,
                                                      "c-continue_unfinished": "on", "c-enabled": "on"})
        auto = AutoRun.objects.get(name="Weekly sales")
        self.assertRedirects(r, reverse("admin:scraper_autorun_change", args=[auto.pk]), fetch_redirect_response=False)
        self.assertEqual((auto.centers, auto.reports, auto.speed, auto.granularity, auto.sales_chunk, auto.fill_missing,
                          auto.retry_empty, auto.recheck_known, auto.period_rule, auto.enabled),
                         (self.list, ["sales", "gym"], "fast", "week", "day", True, True, True, "prev_week", True))
        self.assertEqual(sorted(auto.gyms.values_list("pk", flat=True)), [4])
        self.assertEqual(list(auto.groups.values_list("name", flat=True)), ["GYMS"])
        from django_q.models import Schedule
        self.assertTrue(Schedule.objects.filter(name=autorun.schedule_name(auto)).exists())  # attached to the scheduler
        page = self.client.post(reverse("console:run"), {**post, "save_campaign": "1", "c-name": ""})
        self.assertEqual(page.status_code, 200)  # a missing name is shown on the page, nothing is saved
        self.assertEqual(AutoRun.objects.filter(name="").count(), 0)
        self.assertEqual(self.client.get(reverse("console:run")).status_code, 200)
