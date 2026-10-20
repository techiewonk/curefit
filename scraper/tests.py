import csv
import os
import threading
import time

from django.test import TransactionTestCase

from centers.models import Center, KnownEmpty

from scraper import engine
from scraper.testing import FakeKronosMixin
from scraper.models import (CardRow, GymMember, PerfMetric, SalesRecord, SectionResult, Session)
from scraper.reports import REPORTS

ALL = list(REPORTS)


class EngineTests(FakeKronosMixin, TransactionTestCase):
    """The engine with the network faked: centers 1..4, sales returns `center_id` rows."""

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

    def test_pause_holds_the_run_until_resumed(self):
        status = {}
        out = {}
        self.mode["hook"] = lambda name, cid: status.update(pause=True)  # Pause pressed during the first fetch
        t = threading.Thread(target=lambda: out.update(s=engine.run_session(self.new(["sales"]), status=status)))
        t.start()
        time.sleep(0.6)
        self.assertEqual(len(self.calls), 1)  # the request in flight finished, then the run held
        self.assertTrue(t.is_alive())
        self.mode.clear()
        status["pause"] = False  # Resume
        t.join(timeout=20)
        self.assertEqual(out["s"].status, Session.COMPLETE)
        self.assertEqual(len(self.calls), 4)

    def test_report_columns_are_stable_and_dashboard_escapes_names(self):
        Center.objects.filter(pk=1).update(name='</script><b>boom')
        s = engine.run_session(self.new(["sales", "perf"]))
        with open(os.path.join(s.folder, "sales_report.csv"), newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))
        self.assertEqual(header, ["center_id", "center_name", "center_code", "start_date", "end_date", "user_id",
                                  "name", "phone", "pack_name", "member_end_date", "active_last_30d", "renewed",
                                  "revenue", "scraped_at"])
        with open(os.path.join(s.folder, "dashboard.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertNotIn("<b>boom", html)           # table cells are escaped
        self.assertNotIn("</script><b>", html)      # and the embedded chart data cannot close the script tag
        self.assertIn("&lt;/script&gt;&lt;b&gt;boom", html)

    # ------------------------------------------------------------------ known-empty + adaptive
    def test_known_empty_is_not_asked_again(self):
        KnownEmpty.objects.create(center_id=2, report="sales")
        s = engine.run_session(self.new(["sales"]))
        self.assertNotIn(("sales", 2), self.calls)
        r = SectionResult.objects.get(session=s, center_id=2, report="sales")
        self.assertEqual((r.status, r.error), ("empty", "known empty: not asked"))
        self.assertEqual(s.status, Session.COMPLETE)
        self.assertEqual(s.coverage[0]["known_empty"], 1)

    def test_recheck_known_asks_anyway(self):
        KnownEmpty.objects.create(center_id=2, report="sales")
        engine.run_session(self.new(["sales"]), recheck_known=True)
        self.assertIn(("sales", 2), self.calls)

    def test_empty_in_two_sessions_registers_the_center_as_known_empty(self):
        self.mode["empty2"] = True
        engine.run_session(self.new(["sales"]))
        self.assertFalse(KnownEmpty.objects.exists())  # one empty result proves nothing
        engine.run_session(self.new(["sales"]))
        self.assertTrue(KnownEmpty.objects.filter(center_id=2, report="sales").exists())

    def test_adaptive_run_slows_down_when_a_card_times_out(self):
        self.mode["slow"] = ("gym",)
        status = {}
        engine.run_session(self.new(["sales", "gym"]), workers=2, status=status, adaptive=True)
        self.assertEqual(status["workers"], 1)  # halved from 2 after the first timeout

    def test_fixed_run_keeps_its_speed_whatever_happens(self):
        self.mode["slow"] = ("gym",)
        status = {}
        engine.run_session(self.new(["sales", "gym"]), workers=2, status=status)
        self.assertEqual(status["workers"], 2)

    def test_a_report_empty_for_every_center_never_makes_anyone_known_empty(self):
        self.mode["empty_all"] = True  # e.g. the sales card on a period older than its 90-day window
        engine.run_session(self.new(["sales"]))
        engine.run_session(self.new(["sales"]))
        self.assertFalse(KnownEmpty.objects.exists())
        self.mode.clear()
        s = engine.run_session(self.new(["sales"]))  # a recent period then works for everyone
        self.assertEqual(s.status, Session.COMPLETE)
        self.assertNotIn("known empty: not asked", list(SectionResult.objects.filter(session=s).values_list("error", flat=True)))
