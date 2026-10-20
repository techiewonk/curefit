"""Second-round engine checks: gaps left after the first round of fixes (G3, G4, G11, G13, G16, G24, G25, G1)."""
import datetime as dt
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from centers import services
from scraper import auth, autorun, coverage, engine, files, jobs, login
from scraper.models import AutoRun, Job, Session
from scraper.testing import FakeKronosMixin


class OfflineMessageTests(FakeKronosMixin, TransactionTestCase):
    """G3/G25: a run that cannot start says whether the login was refused or Kronos could not be reached."""

    def setUp(self):
        super().setUp()
        services.set_list("test", [1, 2])
        self.addCleanup(auth._cache.update, at=0.0, state=auth.REJECTED)
        self.spec = {"start": "2026-07-01", "end": "2026-07-31", "list": "test"}

    def refuse(self, why):
        auth._cache.update(at=0.0, state=why)  # what the (patched) check found
        return mock.patch.object(auth, "logged_in", lambda fresh=False: False)

    def test_enqueue_says_offline_instead_of_not_logged_in(self):
        with self.refuse(auth.UNREACHABLE), self.assertRaises(jobs.JobError) as cm:
            jobs.enqueue(self.spec)
        self.assertIn("could not be reached", str(cm.exception))
        self.assertEqual(cm.exception.code, 403)
        with self.refuse(auth.REJECTED), self.assertRaises(jobs.JobError) as cm:
            jobs.enqueue(self.spec)
        self.assertIn("Not logged in", str(cm.exception))
        self.assertFalse(Session.objects.exists())

    def test_fetch_missing_and_automatic_runs_say_so_too(self):
        with self.refuse(auth.UNREACHABLE):
            with self.assertRaises(coverage.CannotFetch) as cm:
                coverage.fetch_missing(center_ids_=[1])
            self.assertIn("could not be reached", str(cm.exception))
            from centers.models import CenterList
            auto = AutoRun.objects.create(name="Daily", frequency=AutoRun.DAILY, run_at=dt.time(6, 30),
                                          centers=CenterList.objects.get(name="test"), reports=["sales"], enabled=True)
            status, message = autorun.run(auto, force=True)
        self.assertEqual(status, "skipped")
        self.assertIn("could not be reached", message)

    def test_junk_center_ids_are_refused_with_a_message(self):
        with self.assertRaises(jobs.JobError) as cm:
            jobs.enqueue({**self.spec, "center_ids": ["1", "abc"]})
        self.assertIn("whole numbers", str(cm.exception))
        self.assertFalse(Session.objects.exists())


class AutoRunAfterAKilledJobTests(FakeKronosMixin, TransactionTestCase):
    """G11: a job that never reaches its own end (runner killed, Stop now) still ends 'started' on the AutoRun."""

    def setUp(self):
        super().setUp()
        services.set_list("test", [1, 2])
        from centers.models import CenterList
        self.auto = AutoRun.objects.create(name="Daily", frequency=AutoRun.DAILY, run_at=dt.time(6, 30),
                                           centers=CenterList.objects.get(name="test"), reports=["sales"],
                                           enabled=True)
        q = mock.patch("scraper.jobs.async_task")
        q.start()
        self.addCleanup(q.stop)
        self.assertEqual(autorun.run(self.auto)[0], "started")
        self.job = Job.objects.get()

    def test_a_job_whose_runner_died_ends_the_automatic_run(self):
        Job.objects.filter(pk=self.job.pk).update(state=Job.RUNNING, pid=None,
                                                  heartbeat=timezone.now() - timedelta(minutes=5))
        Session.objects.filter(pk=self.job.session_id).update(status=Session.RUNNING)
        jobs.refresh_states()
        self.auto.refresh_from_db()
        self.assertEqual(self.auto.last_status, "incomplete")
        self.assertIn("job runner stopped", self.auto.last_message)

    def test_stop_now_ends_the_automatic_run(self):
        Job.objects.filter(pk=self.job.pk).update(state=Job.RUNNING, pid=None, heartbeat=timezone.now())
        Session.objects.filter(pk=self.job.session_id).update(status=Session.RUNNING)
        jobs.force_stop(Job.objects.get(pk=self.job.pk))
        self.auto.refresh_from_db()
        self.assertEqual(self.auto.last_status, "incomplete")
        self.assertIn("Stopped immediately", self.auto.last_message)


class SessionFolderTests(FakeKronosMixin, TransactionTestCase):
    """G1: the folder's session.json is a convenience; a folder that cannot be written never stops a run."""

    def test_a_session_json_that_cannot_be_written_does_not_stop_the_run(self):
        with mock.patch.object(files, "write_session_json", side_effect=OSError("path too long")):
            s = engine.new_session("2026-07-01", "2026-07-31", [1, 2], ["sales"])
            s = engine.run_session(s)
        self.assertEqual(s.status, Session.COMPLETE)


class SendOtpTests(TestCase):
    """G4: a pending login that cannot be kept on disk is a message, not a 500."""

    def test_a_read_only_folder_while_sending_the_otp_is_a_message(self):
        with mock.patch.object(login, "send_otp", side_effect=PermissionError("read-only")):
            result = auth.send_otp()
        self.assertFalse(result)
        self.assertIn("could not be saved", result.message)


class BackupFailureTests(TransactionTestCase):
    """G24: a backup that fails says so and leaves no half copy behind."""

    def test_a_failed_backup_leaves_nothing(self):
        from scraper.management.commands import dbbackup
        folder = Path(tempfile.mkdtemp())
        real_connect = sqlite3.connect

        def connect(path, **kw):
            if str(path).endswith(".partial"):  # the copy: half of it is written, then the disk fails
                Path(path).write_bytes(b"half")
                return mock.Mock()
            db = real_connect(path, **kw)
            source = mock.Mock(wraps=db)
            source.close = db.close
            source.backup.side_effect = sqlite3.OperationalError("disk I/O error")
            return source
        fake = mock.Mock(connect=connect, Error=sqlite3.Error)
        with mock.patch.object(dbbackup, "sqlite3", fake), self.assertRaises(CommandError) as cm:
            call_command("dbbackup", "--dir", str(folder))
        self.assertIn("nothing was written", str(cm.exception))
        self.assertEqual(list(folder.iterdir()), [])
