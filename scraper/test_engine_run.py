"""The scrape, its jobs and its automatic runs: behaviour that broke or was fixed without a test before."""
import datetime as dt
import threading
import time
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from django_q.models import Schedule

from centers import services
from centers.models import KnownEmpty
from scraper import autorun, coverage, customers, engine, files, jobs, kronos, masters, tasks
from scraper.models import AutoRun, Job, SalesCoverage, SalesRecord, SectionResult, Session
from scraper.testing import FakeKronosMixin


def recent_days(n, ago=3):
    """n consecutive days ending `ago` days before today (inside the 90-day sales window)."""
    last = timezone.localdate() - dt.timedelta(days=ago)
    return last - dt.timedelta(days=n - 1), last


class AfterScrapeTests(FakeKronosMixin, TransactionTestCase):
    """G1/G2: what happens after the data is in."""

    def new(self, reports=("sales", "perf")):
        return engine.new_session("2026-07-01", "2026-07-31", [1, 2, 3, 4], list(reports))

    def test_a_csv_that_cannot_be_written_keeps_the_section_ok(self):
        status = {}
        with mock.patch.object(files, "write_center_file", side_effect=PermissionError("locked by Excel")):
            s = engine.run_session(self.new(), status=status)
        self.assertEqual(s.status, Session.COMPLETE)
        self.assertEqual(SectionResult.objects.filter(session=s, status="ok").count(), 8)
        self.assertFalse(SectionResult.objects.filter(session=s, status="failed").exists())
        self.assertTrue(any("CSV was not written" in line for line in status["log"]))

    def test_locked_report_files_never_skip_the_data_steps(self):
        order = []
        with mock.patch.object(files, "export_reports", side_effect=PermissionError("sales_report.csv is open")), \
                mock.patch.object(masters, "merge_session", side_effect=lambda s: order.append("masters")), \
                mock.patch.object(files, "merge_session", side_effect=lambda s: order.append("files")):
            s = engine.run_session(self.new())
        self.assertEqual(s.status, Session.COMPLETE)  # the files are secondary
        self.assertEqual(order, ["masters", "files"])  # data first, then files, each on its own
        self.assertTrue(any("Report files" in c["text"] and c["level"] == "warning" for c in s.checks))

    def test_a_failed_data_step_is_reported_and_the_others_still_run(self):
        merged = []
        with mock.patch.object(customers, "link_session", side_effect=RuntimeError("customers table locked")), \
                mock.patch.object(masters, "merge_session", side_effect=lambda s: merged.append(s.pk)):
            status = {}
            s = engine.run_session(self.new(), status=status)
        self.assertEqual(merged, [s.pk])
        self.assertEqual(s.status, Session.INCOMPLETE)  # not a green "complete" with a table left behind
        errors = [c for c in s.checks if c["level"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Linking customers failed", errors[0]["text"])
        self.assertIn("link_customers", errors[0]["text"])
        self.assertEqual(tasks.outcome(status)[0], Job.DONE)
        self.assertIn("Linking customers", tasks.outcome(status)[1])
        s = engine.run_session(s)  # Continue runs the steps again
        self.assertEqual(s.status, Session.COMPLETE)
        self.assertFalse([c for c in s.checks if c["level"] == "error"])


class EngineBehaviourTests(FakeKronosMixin, TransactionTestCase):
    """G12: engine behaviour that had no test."""

    def test_scrub_hides_the_signed_token(self):
        text = kronos.scrub("GET https://metabase.curefit.co/api/embed/dashboard/eyJh.bG9n.c2ln/dashcard/1 failed")
        self.assertNotIn("eyJh", text)
        self.assertIn("/dashboard/<token>/dashcard/1", text)

    def test_a_token_rejected_after_use_is_replaced_once(self):
        tokens = []
        with mock.patch.object(kronos, "get_token", lambda http, rep, cid, s, e, cfg=None, **kw: tokens.append(cid) or
                               f"{rep['dashboard']}:{cid}"):
            seen = set()

            def expire(name, cid):
                if name == "gym" and cid == 1 and name not in seen:  # gym reuses perf's token: it expired meanwhile
                    seen.add(name)
                    raise kronos.AuthError("HTTP 401")
            self.mode["hook"] = expire
            s = engine.run_session(engine.new_session("2026-07-01", "2026-07-31", [1, 2], ["perf", "gym"]))
        self.assertEqual(s.status, Session.COMPLETE)
        self.assertEqual(tokens.count(1), 2)  # perf's token, then a fresh one for gym
        self.assertEqual(tokens.count(2), 1)

    def test_an_empty_day_is_asked_again_before_it_counts_as_empty(self):
        first, last = recent_days(3)
        self.mode["empty_once"] = {("sales", 1, str(first))}
        s = engine.new_session(first, last, [1], ["sales"], sales_chunk=Session.DAY)
        s = engine.run_session(s)
        self.assertEqual(self.calls.count(("sales", 1)), 4)  # three days, the first one twice
        self.assertEqual(SalesRecord.objects.filter(session=s).count(), 3)
        self.assertEqual(SalesCoverage.objects.filter(center_id=1).count(), 3)

    def test_day_by_day_keeps_every_finished_day_when_stopped_and_continue_adds_no_duplicates(self):
        first, last = recent_days(5)
        status = {}
        self.mode["hook"] = lambda name, cid: status.update(stop=len(self.calls) >= 3)
        s = engine.run_session(engine.new_session(first, last, [1], ["sales"], sales_chunk=Session.DAY), status=status)
        self.assertEqual(s.status, Session.INCOMPLETE)
        self.assertEqual(SalesCoverage.objects.filter(center_id=1).count(), 3)  # the days that arrived are stored
        self.assertEqual(SalesRecord.objects.filter(session=s).count(), 3)
        self.mode.clear()
        status.clear()
        s = engine.run_session(s, status=status)
        self.assertEqual(s.status, Session.COMPLETE)
        self.assertEqual(SalesRecord.objects.filter(session=s).count(), 5)  # the center started over: no doubles
        self.assertEqual(SalesCoverage.objects.filter(center_id=1).count(), 5)

    def test_rows_and_coverage_of_a_day_are_saved_together(self):
        first, last = recent_days(1)
        with mock.patch.object(coverage, "mark", side_effect=RuntimeError("database is locked")):
            s = engine.run_session(engine.new_session(first, last, [1], ["sales"], sales_chunk=Session.DAY))
        self.assertEqual(SalesRecord.objects.filter(session=s).count(), 0)  # rolled back with the coverage
        self.assertEqual(SectionResult.objects.get(session=s).status, "failed")

    def test_a_worker_that_blows_up_stops_the_whole_run(self):
        real = engine.record_result

        def broken(session, cid, rep, *a, **kw):
            if cid == 2:
                raise RuntimeError("database stayed locked")
            return real(session, cid, rep, *a, **kw)
        status = {}
        KnownEmpty.objects.create(center_id=2, report="perf")  # recorded outside the per-section try
        with mock.patch.object(engine, "record_result", broken):
            s = engine.run_session(engine.new_session("2026-07-01", "2026-07-31", [1, 2, 3, 4], ["perf"]),
                                   status=status)
        self.assertEqual(s.status, Session.INCOMPLETE)
        self.assertEqual((status["aborted"], status["reason"]), (True, "error"))
        self.assertNotIn(("perf", 3), self.calls)
        self.assertEqual(tasks.outcome(status)[0], Job.ERROR)

    def test_short_or_day_by_day_sessions_never_make_a_center_known_empty(self):
        self.mode["empty2"] = True
        for _ in range(2):
            engine.run_session(engine.new_session("2026-07-01", "2026-07-07", [1, 2], ["sales"]))  # a week
        self.assertFalse(KnownEmpty.objects.exists())
        for _ in range(2):  # four weeks or more counts
            engine.run_session(engine.new_session("2026-07-01", "2026-07-28", [1, 2], ["sales"]))
        self.assertTrue(KnownEmpty.objects.filter(center_id=2, report="sales").exists())

    def test_the_known_empty_rule_needs_four_weeks_even_when_the_period_is_day_by_day(self):
        self.mode["empty2"] = True
        first, last = recent_days(30)
        for _ in range(2):
            engine.run_session(engine.new_session(first, last, [1, 2], ["sales"], sales_chunk=Session.DAY))
        self.assertFalse(KnownEmpty.objects.exists())


class JobTaskTests(FakeKronosMixin, TransactionTestCase):
    """G3, G14, G15, G18, G22: jobs and the task that runs them."""

    def setUp(self):
        super().setUp()
        services.set_list("test", [1, 2, 3, 4])
        self.spec = {"start": "2026-07-01", "end": "2026-07-31", "list": "test", "workers": 1}

    def test_a_login_abort_ends_the_job_with_an_error_saying_what_to_do(self):
        self.mode["auth"] = True
        job = jobs.enqueue({**self.spec, "only": ["sales"]}, sync=True)
        job.refresh_from_db()
        self.assertEqual(job.state, Job.ERROR)
        self.assertIn("Kronos login rejected 3 times", job.error)
        self.assertIn("Continue", job.error)

    def test_a_stopped_run_says_so(self):
        self.assertEqual(tasks.outcome({"stopped": True, "reason": "stop"}), (Job.DONE, "Stopped by you. "
                                                                                  "Continue the session to fetch the rest."))
        self.assertEqual(tasks.outcome({}), (Job.DONE, ""))

    def test_job_errors_never_carry_the_token(self):
        with mock.patch.object(engine, "run_session",
                               side_effect=RuntimeError("500 for https://metabase.curefit.co/api/embed/dashboard/SECRET.tok/x")):
            job = jobs.enqueue({**self.spec, "only": ["sales"]}, sync=True)
        job.refresh_from_db()
        self.assertEqual(job.state, Job.ERROR)
        self.assertNotIn("SECRET", job.error)
        self.assertIn("<token>", job.error)

    def test_a_job_that_cannot_even_be_read_does_not_crash_the_runner(self):
        tasks.run_job("no-such-job")  # logged, not raised (the runner would retry it otherwise)
        Job.objects.create(id="q1", state=Job.QUEUED)  # no session: fails right away, ends as ERROR, not "queued"
        tasks.run_job("q1")
        self.assertEqual(Job.objects.get(pk="q1").state, Job.ERROR)

    def test_the_heartbeat_revives_a_job_wrongly_written_off(self):
        seen = {}

        def stall(name, cid):
            if not seen:
                job = Job.objects.get()
                Job.objects.filter(pk=job.pk).update(state=Job.INTERRUPTED)
                time.sleep(2.5)  # the heartbeat looks every second
                seen["state"] = Job.objects.get(pk=job.pk).state
        self.mode["hook"] = stall
        job = jobs.enqueue({**self.spec, "only": ["sales"]}, sync=True)
        self.assertEqual(seen["state"], Job.RUNNING)
        job.refresh_from_db()
        self.assertEqual(job.state, Job.DONE)

    def test_a_job_never_picked_up_is_dropped(self):
        Job.objects.create(id="old", state=Job.QUEUED, created_at=timezone.now() - timedelta(minutes=11))
        self.assertIsNone(jobs.active_job())
        job = Job.objects.get(pk="old")
        self.assertEqual(job.state, Job.CANCELLED)
        self.assertIn("Not picked up", job.error)

    def test_a_stalled_job_whose_process_is_alive_is_not_written_off(self):
        Job.objects.create(id="slow", state=Job.RUNNING, pid=jobs.os.getpid(),
                           heartbeat=timezone.now() - timedelta(minutes=2))
        self.assertEqual(jobs.active_job().pk, "slow")
        Job.objects.filter(pk="slow").update(heartbeat=timezone.now() - timedelta(minutes=20))  # hung for too long
        self.assertIsNone(jobs.active_job())
        Job.objects.create(id="gone", state=Job.RUNNING, pid=None, heartbeat=timezone.now() - timedelta(minutes=2))
        self.assertIsNone(jobs.active_job())
        self.assertEqual(Job.objects.get(pk="gone").state, Job.INTERRUPTED)

    def test_a_session_left_running_by_a_killed_process_becomes_interrupted(self):
        old = timezone.now() - timedelta(hours=2)
        Session.objects.create(session_id="dead01", start_date="2026-07-01", end_date="2026-07-31",
                               status=Session.RUNNING, created_at=old)
        alive = Session.objects.create(session_id="busy01", start_date="2026-07-01", end_date="2026-07-31",
                                       status=Session.RUNNING, created_at=old)
        SectionResult.objects.create(session=alive, center_id=1, report="sales", status="ok")  # saved just now
        jobs.refresh_states()
        self.assertEqual(Session.objects.get(pk="dead01").status, Session.INTERRUPTED)
        self.assertEqual(Session.objects.get(pk="busy01").status, Session.RUNNING)

    def test_two_starts_at_once_make_one_job(self):
        results, barrier = [], threading.Barrier(2)

        def start():
            try:
                barrier.wait()
                results.append(jobs.enqueue(self.spec).pk)
            except jobs.JobError as e:
                results.append(e.code)
            finally:
                connection.close()
        with mock.patch("scraper.jobs.async_task"):
            threads = [threading.Thread(target=start) for _ in range(2)]
            [t.start() for t in threads]
            [t.join(30) for t in threads]
        self.assertEqual(Job.objects.count(), 1)
        self.assertIn(409, results)

    def test_retry_empty_keeps_the_empty_results_when_the_job_cannot_be_made(self):
        self.mode["empty2"] = True
        job = jobs.enqueue({**self.spec, "only": ["sales"]}, sync=True)
        with mock.patch.object(Job.objects, "create", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                jobs.enqueue({"session": job.session_id, "retry_empty": True})
        self.assertTrue(SectionResult.objects.filter(session_id=job.session_id, status="empty").exists())

    def test_run_scrape_goes_through_a_job_and_reports_by_exit_code(self):
        out = StringIO()
        call_command("run_scrape", "--start", "2026-07-01", "--end", "2026-07-31", "--list", "test", "--only", "sales",
                     "--speed", "fast", "--granularity", "week", stdout=out)
        job = Job.objects.get()
        self.assertEqual((job.state, job.session.status, job.session.granularity), (Job.DONE, Session.COMPLETE, "week"))
        self.mode["fail"] = ("perf",)
        with self.assertRaises(SystemExit) as cm:
            call_command("run_scrape", "--start", "2026-07-01", "--end", "2026-07-31", "--list", "test",
                         "--only", "perf", stdout=out)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("--session", out.getvalue())
        with self.assertRaises(CommandError):
            call_command("run_scrape", "--start", "2026-07-32", "--end", "2026-07-31", stdout=out)
        Job.objects.create(id="busy", state=Job.RUNNING, heartbeat=timezone.now())
        with self.assertRaises(CommandError):  # one scrape at a time covers the command line too
            call_command("run_scrape", "--start", "2026-07-01", "--end", "2026-07-31", "--list", "test", stdout=out)


class AutoRunLifecycleTests(FakeKronosMixin, TransactionTestCase):
    """G11, G15: automatic runs follow their job to the end."""

    def setUp(self):
        super().setUp()
        services.set_list("test", [1, 2])
        from centers.models import CenterList
        self.list = CenterList.objects.get(name="test")
        self.q = mock.patch("scraper.jobs.async_task")
        self.q.start()
        self.addCleanup(self.q.stop)

    def auto(self, **kw):
        fields = dict(name="Daily", frequency=AutoRun.DAILY, run_at=dt.time(6, 30), centers=self.list,
                      reports=["sales"], speed="normal", granularity=Session.DAY, enabled=True)
        fields.update(kw)
        return AutoRun.objects.create(**fields)

    def test_the_status_follows_the_job_to_its_end(self):
        auto = self.auto()
        self.assertEqual(autorun.run(auto)[0], "started")
        tasks.run_job(Job.objects.get().pk)
        auto.refresh_from_db()
        self.assertEqual(auto.last_status, "finished")
        self.assertIn("complete", auto.last_message)

    def test_a_run_that_meets_another_scrape_waits_and_starts_when_it_ends(self):
        auto = self.auto()
        Job.objects.create(id="other", state=Job.RUNNING, heartbeat=timezone.now())
        self.assertEqual(autorun.run(auto)[0], "waiting")
        Job.objects.filter(pk="other").update(state=Job.DONE)
        autorun.start_waiting()
        auto.refresh_from_db()
        self.assertEqual(auto.last_status, "started")
        self.assertEqual(Job.objects.filter(state=Job.QUEUED).count(), 1)

    def test_a_skipped_start_keeps_the_unfinished_session(self):
        auto = self.auto()
        session = Session.objects.create(session_id="unfin1", start_date="2026-07-01", end_date="2026-07-31",
                                         status=Session.INCOMPLETE)
        AutoRun.objects.filter(pk=auto.pk).update(last_session=session)
        auto.refresh_from_db()
        with mock.patch("scraper.auth.logged_in", lambda fresh=False: False):
            self.assertEqual(autorun.run(auto)[0], "skipped")
        auto.refresh_from_db()
        self.assertEqual(auto.last_session_id, "unfin1")

    def test_saving_a_campaign_keeps_a_due_run_unless_its_timing_changed(self):
        auto = self.auto()
        overdue = timezone.make_aware(dt.datetime.combine(timezone.localdate() - dt.timedelta(days=1), dt.time(6, 30)))
        Schedule.objects.filter(name=autorun.schedule_name(auto)).update(next_run=overdue)
        auto.reports = ["sales", "perf"]
        auto.save()
        self.assertEqual(Schedule.objects.get(name=autorun.schedule_name(auto)).next_run, overdue)
        auto.run_at = dt.time(7, 15)
        auto.save()
        planned = timezone.localtime(Schedule.objects.get(name=autorun.schedule_name(auto)).next_run)
        self.assertEqual((planned.hour, planned.minute), (7, 15))
        self.assertGreater(planned, timezone.now())

    def test_auto_run_wait_is_a_job_and_honours_retry_empty(self):
        self.q.stop()  # --wait runs the job here and now, through the real queue call
        self.mode["empty2"] = True
        auto = self.auto(period_rule=AutoRun.FIXED, start_date=dt.date(2026, 7, 1), end_date=dt.date(2026, 7, 31),
                         retry_empty=True)
        result = autorun.run(auto, force=True, inline=True)
        self.assertEqual(result[0], "finished", (result, list(Session.objects.values_list("checks", "missing"))))
        self.assertEqual(Job.objects.count(), 1)
        self.mode.clear()
        self.calls.clear()
        Session.objects.update(status=Session.INCOMPLETE)  # pretend it did not finish: the next run continues it
        self.assertEqual(autorun.run(auto, force=True, inline=True)[0], "finished")
        self.assertEqual(self.calls, [("sales", 2)])  # the empty result was asked again

    def test_next_occurrence_at_month_ends(self):
        monthly = self.auto(name="M", frequency=AutoRun.MONTHLY, month_day=28)
        at = lambda *a: timezone.make_aware(dt.datetime(*a))  # noqa: E731
        self.assertEqual(autorun.next_occurrence(monthly, at(2026, 1, 31, 12)), at(2026, 2, 28, 6, 30))
        self.assertEqual(autorun.next_occurrence(monthly, at(2026, 12, 31, 12)), at(2027, 1, 28, 6, 30))
        self.assertEqual(autorun.next_occurrence(monthly, at(2026, 2, 28, 6)), at(2026, 2, 28, 6, 30))
        weekly = self.auto(name="W", frequency=AutoRun.WEEKLY, weekday=0)
        self.assertEqual(autorun.next_occurrence(weekly, at(2026, 12, 28, 7)), at(2027, 1, 4, 6, 30))  # a Monday, too late
        daily = self.auto(name="D")
        self.assertEqual(autorun.next_occurrence(daily, at(2026, 12, 31, 23)), at(2027, 1, 1, 6, 30))


class CoverageTests(FakeKronosMixin, TransactionTestCase):
    """G12/G20: which sales days are stored and which still need asking."""

    today = dt.date(2026, 10, 21)

    def test_pieces(self):
        a, b = dt.date(2026, 10, 1), dt.date(2026, 10, 10)
        self.assertEqual(coverage.pieces(a, b, Session.WHOLE), [(a, b)])
        self.assertEqual(len(coverage.pieces(a, b, Session.DAY)), 10)
        self.assertEqual(coverage.pieces(a, b, Session.WEEK), [(a, dt.date(2026, 10, 7)), (dt.date(2026, 10, 8), b)])

    def test_to_fetch_skips_stored_days_and_days_before_the_window(self):
        s = Session.objects.create(session_id="cov1", start_date=dt.date(2026, 7, 20), end_date=dt.date(2026, 7, 25),
                                   sales_chunk=Session.DAY, fill_missing=True)
        coverage.mark(s, 1, [(dt.date(2026, 7, 24), dt.date(2026, 7, 24))], today=self.today)
        todo = coverage.to_fetch(s, 1, today=self.today)  # the window starts on 2026-07-23
        self.assertEqual(todo, [(dt.date(2026, 7, 23),) * 2, (dt.date(2026, 7, 25),) * 2])
        s.fill_missing = False
        self.assertEqual(len(coverage.to_fetch(s, 1, today=self.today)), 6)

    def test_mark_records_only_finished_days_inside_the_window(self):
        s = Session.objects.create(session_id="cov2", start_date=dt.date(2026, 7, 1), end_date=self.today)
        coverage.mark(s, 1, [(dt.date(2026, 7, 21), self.today)], today=self.today)
        days = set(SalesCoverage.objects.values_list("day", flat=True))
        self.assertEqual(min(days), dt.date(2026, 7, 23))
        self.assertEqual(max(days), self.today - dt.timedelta(days=1))  # today is not over yet

    def test_known_empty_centers_are_not_counted_or_fetched_as_missing(self):
        KnownEmpty.objects.create(center_id=2, report="sales")
        self.assertEqual({d["missing"] for d in coverage.missing_by_day([1, 2])}, {1})
        self.assertEqual([g["id"] for g in coverage.missing_by_center([1, 2])], [1])
        with mock.patch("scraper.jobs.async_task"):
            job = coverage.fetch_missing(center_ids_=[1, 2])
        self.assertEqual(job.session.center_ids, [1])
        Job.objects.update(state=Job.DONE)
        with self.assertRaises(coverage.CannotFetch):
            coverage.fetch_missing(center_ids_=[2])
