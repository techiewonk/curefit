from unittest import mock

from datetime import timedelta

from django.test import TransactionTestCase
from django.utils import timezone

from centers import services
from scraper import engine, jobs
from scraper.models import Job, SalesRecord, SectionResult, Session
from scraper.testing import FakeKronosMixin


class JobTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        services.set_list("test", [1, 2, 3, 4])
        self.spec = {"start": "2026-07-01", "end": "2026-07-31", "list": "test", "workers": 2}

    def run_job(self, **extra):
        return jobs.enqueue({**self.spec, **extra}, sync=True)  # q2 runs the task inline

    def test_job_runs_to_completion(self):
        job = self.run_job(only=["sales", "perf"])
        job.refresh_from_db()
        self.assertEqual((job.state, job.done, job.total), (Job.DONE, 8, 8))
        self.assertEqual(job.session.status, Session.COMPLETE)
        self.assertEqual(SalesRecord.objects.filter(session=job.session).count(), 10)
        self.assertTrue(any("ok" in line for line in job.log))
        self.assertIsNotNone(job.pid)
        self.assertEqual(job.active, [])

    def test_one_job_at_a_time(self):
        Job.objects.create(id="busy", state=Job.RUNNING, heartbeat=timezone.now())
        with self.assertRaises(jobs.JobError) as cm:
            jobs.enqueue(self.spec, sync=True)
        self.assertEqual(cm.exception.code, 409)

    def test_validation(self):
        for bad in ({"only": ["nope"]}, {"workers": 9}, {"list": "actual"}, {"start": "2026-08-01"}):
            with self.assertRaises(jobs.JobError, msg=str(bad)):
                jobs.enqueue({**self.spec, **bad}, sync=True)

    def test_continue_after_a_stop_finishes_the_session(self):
        s = engine.new_session("2026-07-01", "2026-07-31", [1, 2, 3, 4], ["sales", "perf"])
        status = {"stop": False}
        self.mode["hook"] = lambda n, c: status.update(stop=len(self.calls) >= 4)
        engine.run_session(s, status=status)
        s.refresh_from_db()
        self.assertEqual(s.status, Session.INCOMPLETE)
        self.mode.clear()
        job = jobs.enqueue({"session": s.session_id, "workers": 1}, sync=True)
        job.refresh_from_db()
        self.assertEqual(job.state, Job.DONE)
        self.assertEqual(Session.objects.get(pk=s.pk).status, Session.COMPLETE)

    def test_section_errors_do_not_crash_the_job(self):
        self.mode["fail"] = ("sales",)
        job = self.run_job(only=["sales"])
        job.refresh_from_db()
        self.assertEqual(job.state, Job.DONE)  # a failed section is data, not a crashed job
        self.assertEqual(job.session.status, Session.INCOMPLETE)
        self.assertTrue(SectionResult.objects.filter(session=job.session, status="failed").exists())

    def test_stale_running_job_becomes_interrupted(self):
        s = Session.objects.create(session_id="abc123", start_date="2026-07-01", end_date="2026-07-31",
                                   status=Session.RUNNING)
        Job.objects.create(id="old", state=Job.RUNNING, session=s,
                           heartbeat=timezone.now() - timedelta(minutes=5))
        self.assertIsNone(jobs.active_job())
        self.assertEqual(Job.objects.get(pk="old").state, Job.INTERRUPTED)
        self.assertEqual(Session.objects.get(pk="abc123").status, Session.INTERRUPTED)
        # an interrupted session is offered for continuing
        self.assertEqual(engine.find_unfinished("2026-07-01", "2026-07-31").pk, "abc123")

    def test_retry_empty_asks_again(self):
        self.mode["empty2"] = True
        job = self.run_job(only=["sales"])
        sid = job.session_id
        self.assertEqual(SectionResult.objects.get(session_id=sid, center_id=2, report="sales").status, "empty")
        self.mode.clear()
        self.calls.clear()
        jobs.enqueue({"session": sid, "retry_empty": True, "workers": 1}, sync=True)
        self.assertEqual(self.calls, [("sales", 2)])
        self.assertEqual(SalesRecord.objects.filter(session_id=sid, center_id=2).count(), 2)

    def test_pause_stop_and_payload(self):
        job = Job.objects.create(id="p1", state=Job.RUNNING, heartbeat=timezone.now())
        jobs.set_pause(job, True)
        self.assertTrue(jobs.payload(job)["paused"])
        jobs.set_pause(job, False)
        self.assertFalse(jobs.payload(job)["paused"])
        jobs.request_stop(job)
        self.assertTrue(jobs.payload(job)["stopping"])
        self.assertEqual(jobs.payload(None)["state"], "idle")


class PickedCentersTests(FakeKronosMixin, TransactionTestCase):
    def test_chosen_centers_replace_the_list(self):
        services.set_list("test", [1, 2, 3, 4])
        with mock.patch("scraper.jobs.async_task"):
            job = jobs.enqueue({"start": "2026-07-01", "end": "2026-07-31", "list": "test", "workers": 1,
                                "center_ids": [3, 1]})
        self.assertEqual(job.session.center_ids, [1, 3])

    def test_run_form_counts_and_sends_the_picked_centers(self):
        from console.forms import RunForm
        services.set_list("test", [1, 2, 3, 4])
        form = RunForm({"start": "2026-07-01", "end": "2026-07-31", "centers": "test", "speed": "normal",
                        "reports": ["sales"], "pick": [2, 4]})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.center_count, 2)
        self.assertEqual(form.to_spec()["center_ids"], [2, 4])
