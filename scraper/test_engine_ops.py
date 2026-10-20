"""Start-up, backups, packages and the files a run writes (G13, G23, G24, G35, G57)."""
import csv
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest import TestCase, mock, skipUnless

import tablib
from django.conf import settings
from django.core.management import call_command
from django.test import TransactionTestCase

from console.management.commands import serve
from scraper import engine, files, storage
from scraper.models import PerfMetric
from scraper.testing import FakeKronosMixin


class ServeTests(TestCase):
    def test_stop_never_raises_when_the_runner_does_not_end(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("qcluster", 15), subprocess.TimeoutExpired("qcluster", 5)]
        with mock.patch.object(serve, "_kill_tree"):
            serve.stop(process)
        process.kill.assert_called_once()

    def test_a_failing_startup_step_does_not_stop_the_others(self):
        out, ran = [], []

        def command(name, *a, **kw):
            ran.append(name)
            if name == "merge_masters":
                raise RuntimeError("database is locked")
        commands = [s for s in serve.STARTUP_STEPS if isinstance(s, str)]
        with mock.patch.object(serve, "call_command", command):
            failed = serve.run_startup_steps(out.append, steps=commands)
        self.assertEqual(ran, commands)
        self.assertEqual(failed, ["merge_masters"])
        self.assertIn("python manage.py merge_masters", out[0])

    def test_the_one_time_repair_runs_first_at_start_and_a_failure_is_reported(self):
        self.assertIs(serve.STARTUP_STEPS[0][1], serve._repair_once)
        out, ran = [], []
        broken = mock.Mock(error="RuntimeError: customers table locked")
        with mock.patch("scraper.repairs.run_once", return_value=broken), \
                mock.patch.object(serve, "call_command", lambda name, *a, **kw: ran.append(name)):
            failed = serve.run_startup_steps(out.append)
        self.assertEqual(failed, ["one-time data repair"])
        self.assertIn("tried again at the next start", out[0])
        self.assertEqual(ran, ["link_customers", "merge_masters", "merge_months"])  # the others still ran
        with mock.patch("scraper.repairs.run_once", return_value=None):  # already done: nothing to report
            self.assertEqual(serve.run_startup_steps(out.append, steps=serve.STARTUP_STEPS[:1]), [])

    def test_the_server_starts_before_the_catch_up_steps_finish(self):
        order = []
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "RUN_MAIN"}, clear=True), \
                mock.patch.object(serve, "call_command", lambda name, *a, **kw: order.append(name)), \
                mock.patch.object(serve.threading, "Thread") as thread, \
                mock.patch.object(serve, "checkpoint"), \
                mock.patch("waitress.serve", lambda *a, **kw: order.append("web server")):
            call_command("serve", "--no-open", "--no-jobs", stdout=StringIO())
        self.assertEqual(order, ["migrate", "seed_groups", "web server"])  # no backfill before the server
        self.assertIs(thread.call_args.kwargs["target"], serve.run_startup_steps)  # it runs in the background

    def test_a_left_over_runner_is_ended_and_a_dead_one_forgotten(self):
        pid_file = Path(tempfile.mkdtemp()) / "runner.pid"
        pid_file.write_text("4242")
        with mock.patch.object(serve, "RUNNER_PID", pid_file), mock.patch("scraper.jobs.pid_alive", lambda pid: True), \
                mock.patch.object(serve, "_looks_like_python", lambda pid: True), \
                mock.patch.object(serve, "_kill_tree") as kill:
            self.assertEqual(serve.end_stale_runner(lambda m: None), 4242)
        kill.assert_called_once_with(4242)
        self.assertFalse(pid_file.exists())
        pid_file.write_text("4242")
        with mock.patch.object(serve, "RUNNER_PID", pid_file), mock.patch("scraper.jobs.pid_alive", lambda pid: False), \
                mock.patch.object(serve, "_kill_tree") as kill:
            self.assertIsNone(serve.end_stale_runner(lambda m: None))
        kill.assert_not_called()

    def test_a_dead_runner_is_reported(self):
        runner, out = mock.Mock(), []
        runner.poll.side_effect = [None, 3]
        self.assertEqual(serve.watch_runner(runner, out.append, every=0), 3)
        self.assertIn("job runner stopped", out[0])

    @skipUnless(os.name == "nt", "Windows job objects")
    def test_the_runner_ends_with_this_process(self):
        import ctypes
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            job = serve.tie_to_this_process(child)
            self.assertIsNotNone(job)
            serve._jobs.remove(job)
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(job))  # what happens when this process ends
            self.assertIsNotNone(child.wait(timeout=10))
        finally:
            if child.poll() is None:
                child.kill()


class BackupTests(TransactionTestCase):
    def test_backup_is_one_complete_file(self):
        folder = tempfile.mkdtemp()
        out = StringIO()
        call_command("dbbackup", "--dir", folder, stdout=out)
        call_command("dbbackup", "--dir", folder, "--keep", "1", stdout=out)  # same day: a second name, then pruned
        copies = list(Path(folder).glob("db_*.sqlite3"))
        self.assertEqual(len(copies), 1)
        with sqlite3.connect(copies[0]) as db:
            self.assertGreater(db.execute("select count(*) from django_migrations").fetchone()[0], 0)
        self.assertFalse(list(Path(folder).glob("*.partial")))


class PackageTests(TestCase):
    root = Path(settings.BASE_DIR)

    def test_every_requirement_is_pinned_and_tablib_is_listed(self):
        def names(path):
            return {line.split("=")[0].split(">")[0].strip().lower() for line in (self.root / path).read_text().splitlines()
                    if line.strip() and not line.startswith("#")}
        required, pinned = names("requirements.txt"), names("constraints.txt")
        self.assertIn("tablib", required)
        self.assertLessEqual(required, pinned)

    def test_the_start_scripts_install_changed_packages_and_seed_groups_only_once(self):
        for name in ("run.bat", "dev.bat"):
            text = (self.root / name).read_text()
            self.assertIn("call update_packages.bat", text, name)
            self.assertNotIn("manage.py seed_groups", text, name)  # serve does it
        updater = (self.root / "update_packages.bat").read_text()
        self.assertIn("-c constraints.txt", updater)
        self.assertIn("requirements.stamp", updater)
        self.assertIn("run install.bat", updater)

    def test_secrets_and_sheets_are_git_ignored(self):
        ignored = (self.root / ".gitignore").read_text().splitlines()
        for entry in ("session.tmp", "session.pending.json", "runner.pid", "*.xls", "*.xlsx", "logs/", "backups/",
                      "imports/"):
            self.assertIn(entry, ignored)


class FileTests(FakeKronosMixin, TransactionTestCase):
    def test_csv_cells_cannot_run_as_formulas_and_open_in_excel(self):
        path = os.path.join(self.tmp, "x.csv")
        files.save_csv(path, tablib.Dataset(["=HYPERLINK(\"http://x\")", "-12.5", "+91 98", "@SUM(A1)", "plain", 3],
                                            headers=list("abcdef")))
        raw = Path(path).read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))  # UTF-8 with BOM
        with open(path, newline="", encoding="utf-8-sig") as f:
            row = list(csv.reader(f))[1]
        self.assertEqual(row, ["'=HYPERLINK(\"http://x\")", "-12.5", "'+91 98", "'@SUM(A1)", "plain", "3"])

    def test_perf_pivot_has_one_row_per_center_and_date(self):
        s = engine.new_session("2026-07-01", "2026-07-31", [1], ["perf"])
        for day, value in (("2026-07-01", 10.0), ("2026-07-02", 20.0)):
            PerfMetric.objects.create(session=s, center_id=1, metric_date=day, metric="1. Total Revenue", value=value)
        files.export_reports(s)
        with open(os.path.join(s.folder, "perf_pivot.csv"), newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["center_id", "center_name", "metric_date", "1. Total Revenue"])
        self.assertEqual([r[2:] for r in rows[1:]], [["2026-07-01", "10.0"], ["2026-07-02", "20.0"]])

    def test_numbers_that_are_not_numbers_are_stored_as_empty(self):
        for bad in ("NaN", float("nan"), "inf", float("-inf"), "", None, "n/a"):
            self.assertIsNone(storage._f(bad), bad)
        self.assertEqual(storage._f("12.34567"), 12.3457)
        self.assertTrue(math.isfinite(storage._f(1e300)))
