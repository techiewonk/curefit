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
        self.assertEqual(autorun.period(self.auto(name="w", frequency=AutoRun.WEEKLY), today),
                         (dt.date(2026, 10, 12), dt.date(2026, 10, 18)))
        self.assertEqual(autorun.period(self.auto(name="m", frequency=AutoRun.MONTHLY), today),
                         (dt.date(2026, 9, 1), dt.date(2026, 9, 30)))

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

    def test_queued_run_is_not_repeated_the_same_day(self):
        auto = self.auto(enabled=True)
        with mock.patch.object(jobs, "enqueue", return_value=mock.Mock(session_id="x", session=None)):
            self.assertEqual(autorun.run(auto)[0], "started")
            self.assertEqual(autorun.run(auto), ("skipped", "Already started today."))

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
