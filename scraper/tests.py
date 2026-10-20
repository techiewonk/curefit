import csv
import os
import tempfile
from unittest import mock

from django.test import TransactionTestCase

from centers.models import Center
from scraper import conf, engine, files, kronos
from scraper.models import (CardRow, GymMember, PerfMetric, SalesRecord, SectionResult, Session)
from scraper.reports import REPORTS

ALL = list(REPORTS)


class EngineTests(TransactionTestCase):
    """The engine with the network faked: every center is 1..4, sales returns `center_id` rows."""

    def setUp(self):
        for i in range(1, 5):
            Center.objects.create(center_id=i, name=f"Cult {i}", code=f"C{i}", state="ACTIVE")
        self.tmp = tempfile.mkdtemp()
        self.calls, self.mode = [], {}
        self.by_card = {v["card"]: k for k, v in REPORTS.items()}
        patches = [
            mock.patch.object(files, "RUNS_DIR", self.tmp),
            mock.patch.object(conf, "polite_sleep", lambda *a: None),
            mock.patch.object(engine.C, "load_session", lambda: (object(), {})),
            mock.patch.object(kronos, "get_token", lambda http, rep, cid, s, e, timeout=None: f"{rep['dashboard']}:{cid}"),
            mock.patch.object(kronos, "warm_up", lambda mb, token, timeout=None: None),
            mock.patch.object(kronos, "get_rows", self.fake_rows),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_rows(self, mb, rep, token, start, end, poll_seconds=None, timeout=None):
        cid = int(token.split(":")[1])
        name = self.by_card[rep["card"]]
        self.calls.append((name, cid))
        hook = self.mode.get("hook")
        if hook:
            hook(name, cid)
        if name in self.mode.get("fail", ()):
            raise RuntimeError("HTTP 500")
        if name in self.mode.get("slow", ()):
            raise kronos.SlowQuery("not completed")
        if self.mode.get("auth"):
            raise kronos.AuthError("HTTP 401")
        if name == "sales" and cid == 2 and self.mode.get("empty2"):
            return [], []
        if name == "sales":
            return [[cid, "a", "p", "pk", "d", "Yes", "Yes", 10]] * cid, []
        if name == "perf":
            return [["2026-07-01", "1. Total Revenue", 100.0], ["2026-07-01", "1.a New Revenue", 60.0]], []
        if name == "gym":
            return [["u", "n", "pk", 5, "d", "d", "d", "Active", "Online", "New", "Adv", "d"]], []
        return [[cid, 7]], ["center", "value"]

    def new(self, reports=ALL):
        return engine.new_session("2026-07-01", "2026-07-31", [1, 2, 3, 4], reports)

    def counts(self, s):
        return [m.objects.filter(session=s).count() for m in (SalesRecord, PerfMetric, GymMember, CardRow, SectionResult)]

    # ------------------------------------------------------------------ tests
    def test_full_run_complete(self):
        s = engine.run_session(self.new())
        self.assertEqual(s.status, Session.COMPLETE)
        self.assertEqual(self.counts(s), [10, 8, 4, 60, 72])  # 1+2+3+4 sales rows; 4x2 perf; 4 gym; 15x4 cards
        self.assertEqual(s.missing, {})
        self.assertEqual(len(s.coverage), 18)
        self.assertTrue(os.path.exists(os.path.join(s.folder, "session.json")))

    def test_files_and_merged(self):
        s = engine.run_session(self.new())
        merged = os.path.join(s.folder, "merged")
        with open(os.path.join(merged, "sales.csv"), newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows) - 1, 10)
        self.assertEqual(rows[0][:2], ["center_id", "center_name"])
        self.assertEqual({r[0] for r in rows[1:]}, {"1", "2", "3", "4"})
        self.assertEqual(len(os.listdir(os.path.join(s.folder, "centers"))), 4)
        for name in ("sales_report.csv", "perf_pivot.csv", "gym_members.csv", "analysis.csv", "dashboard.html", "coverage.csv"):
            self.assertTrue(os.path.exists(os.path.join(s.folder, name)), name)
        self.assertTrue(os.path.exists(os.path.join(merged, "all_reports.xlsx")))

    def test_parallel_matches_sequential(self):
        seq = engine.run_session(self.new())
        par = engine.run_session(self.new(), workers=3)
        self.assertEqual(par.status, Session.COMPLETE)
        self.assertEqual(self.counts(seq), self.counts(par))

    def test_repeat_purchases_are_kept(self):
        s = engine.run_session(self.new(["sales"]))
        self.assertEqual(SalesRecord.objects.filter(session=s, center_id=4).count(), 4)  # identical rows, all kept

    def test_refresh_replaces_not_duplicates(self):
        s = engine.run_session(self.new(["sales", "perf"]))
        before = self.counts(s)
        engine.run_session(s, refresh=True)
        self.assertEqual(self.counts(s), before)

    def test_resume_fetches_only_missing(self):
        s = engine.run_session(self.new(["sales", "perf"]))
        SectionResult.objects.filter(session=s, center_id=3, report="perf").delete()
        PerfMetric.objects.filter(session=s, center_id=3).delete()
        self.calls.clear()
        s = engine.run_session(s)
        self.assertEqual(self.calls, [("perf", 3)])
        self.assertEqual(s.status, Session.COMPLETE)
        self.assertEqual(PerfMetric.objects.filter(session=s).count(), 8)

    def test_empty_is_done_and_does_not_block_completion(self):
        self.mode["empty2"] = True
        s = engine.run_session(self.new(["sales"]))
        self.assertEqual(s.status, Session.COMPLETE)
        r = SectionResult.objects.get(session=s, center_id=2, report="sales")
        self.assertEqual((r.status, r.row_count), ("empty", 0))
        self.assertEqual(self.calls.count(("sales", 2)), 2)  # one retry on a fresh token
        self.calls.clear()
        engine.run_session(s)
        self.assertEqual(self.calls, [])  # an empty result is not asked again in the same session

    def test_failed_sections_are_retried_on_resume(self):
        self.mode["fail"] = ("perf",)
        s = engine.run_session(self.new(["sales", "perf"]))
        self.assertEqual(s.status, Session.INCOMPLETE)
        self.assertEqual(SectionResult.objects.filter(session=s, report="perf", status="failed").count(), 3)
        self.assertTrue(s.coverage[1]["skipped_after_failures"])  # skipped after 3 failures in a row
        self.mode.clear()
        s = engine.run_session(s)
        self.assertEqual(s.status, Session.COMPLETE)
        self.assertEqual(SectionResult.objects.filter(session=s, report="perf", status="ok").count(), 4)

    def test_slow_is_recorded_and_retried_later(self):
        self.mode["slow"] = ("gym",)
        s = engine.run_session(self.new(["sales", "gym"]))
        self.assertEqual(SectionResult.objects.filter(session=s, status="slow").count(), 4)
        self.assertEqual(self.calls.count(("gym", 1)), 1)  # a timeout is not retried four times
        self.assertEqual(s.status, Session.INCOMPLETE)
        self.mode.clear()
        self.assertEqual(engine.run_session(s).status, Session.COMPLETE)

    def test_auth_failure_stops_the_run(self):
        self.mode["auth"] = True
        s = engine.run_session(self.new(["sales"]))
        self.assertEqual(len(self.calls), 3)  # fail-fast after three 401/403
        self.assertEqual(s.status, Session.INCOMPLETE)

    def test_stop_request_ends_cleanly(self):
        status = {}
        self.mode["hook"] = lambda name, cid: status.update(stop=(len(self.calls) >= 5))
        s = engine.run_session(self.new(["sales", "perf"]), status=status)
        self.assertEqual(s.status, Session.INCOMPLETE)
        self.assertTrue(status["stopped"])
        self.assertLess(len(self.calls), 8)
        self.mode.clear()
        status.clear()
        self.assertEqual(engine.run_session(s, status=status).status, Session.COMPLETE)

    def test_find_unfinished(self):
        self.mode["fail"] = ("perf",)
        s = engine.run_session(self.new(["sales", "perf"]))
        self.assertEqual(engine.find_unfinished("2026-07-01", "2026-07-31"), s)
        self.mode.clear()
        engine.run_session(s)
        self.assertIsNone(engine.find_unfinished("2026-07-01", "2026-07-31"))

    def test_status_dict_reports_progress(self):
        status = {}
        s = engine.run_session(self.new(["sales"]), status=status)
        self.assertEqual((status["state"], status["done"], status["total"]), ("done", 4, 4))
        self.assertEqual(status["session_status"], "complete")
        self.assertTrue(any("ok" in line for line in status["log"]))
        self.assertEqual(status["active"], [])
