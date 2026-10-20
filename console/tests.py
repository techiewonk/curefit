import json
import os
from unittest import mock

from django.test import Client, TransactionTestCase
from django.utils import timezone

from centers import services as center_services
from centers.models import Center
from console import services
from scraper import engine, jobs
from scraper.models import Job, SectionResult
from scraper.testing import FakeKronosMixin

BASE = "/admin/console/"
PAGES = ["run/", "monitor/", "analytics/", "compare/", "centers/"]


class FakeResp:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body

    def json(self):
        return self._body


class ConsoleTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        center_services.set_list("test", [1, 2, 3, 4])
        center_services.set_list("actual", [1, 2])
        self.session = engine.run_session(engine.new_session("2026-07-01", "2026-07-31", [1, 2, 3, 4], list(engine.REPORTS)))
        self.logged_in = True
        for p in (mock.patch.object(services, "logged_in", lambda fresh=False: self.logged_in),
                  mock.patch.object(services.C, "send_otp", lambda email=None: FakeResp(200)),
                  mock.patch.object(services.C, "validate_otp",
                                    lambda otp, email=None: FakeResp(200, {"token": "x"}) if otp == "123456" else FakeResp(401, {})),
                  mock.patch.object(services.C, "establish_session", lambda: FakeResp(200)),
                  mock.patch.object(services.C, "session_ok", lambda: True)):
            p.start()
            self.addCleanup(p.stop)

    def get(self, url, **params):
        return self.client.get(BASE + url, params)

    def post(self, path, body=None, client=None):
        return (client or self.client).post(BASE + path, json.dumps(body or {}), content_type="application/json")

    # ------------------------------------------------------------------ pages
    def test_pages_render(self):
        for p in PAGES:
            r = self.get(p)
            self.assertEqual(r.status_code, 200, p)
            self.assertIn("console/app.js", r.content.decode())
        home = self.client.get("/admin/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("Needs attention", home.content.decode())
        self.assertContains(home, self.session.session_id)

    # ------------------------------------------------------------------ reads
    def test_read_apis(self):
        sid = self.session.session_id
        cfg = self.get("api/config/").json()
        self.assertEqual(len(cfg["reports"]), 18)
        self.assertEqual(cfg["lists"], {"actual": 2, "test": 4})
        self.assertEqual(self.get("api/sessions/").json()[0]["session"], sid)
        a = self.get("api/analytics/", session=sid).json()
        self.assertEqual((a["totals"]["centers"], a["totals"]["sales_count"]), (4, 10))
        self.assertEqual(a["totals"]["total_revenue"], 400.0)
        d = self.get("api/sales/", session=sid, center_id=3).json()
        self.assertEqual(len(d["rows"]), 3)
        files = self.get("api/session-files/", session=sid).json()["files"]
        self.assertTrue(any(f["path"] == "merged/sales.csv" for f in files))
        self.assertEqual(self.get("api/unfinished/", start="2026-07-01", end="2026-07-31").json()["session"], None)

    def test_monitor_grid_reflects_results(self):
        SectionResult.objects.filter(session=self.session, center_id=2, report="perf").update(status="failed", error="HTTP 500")
        SectionResult.objects.filter(session=self.session, center_id=3, report="gym").update(status="slow", error="timed out")
        SectionResult.objects.filter(session=self.session, center_id=4, report="sales").update(status="empty", row_count=0)
        m = self.get("api/monitor/", session=self.session.session_id).json()
        reps = m["reports"]
        row = {cid: m["grid"][i] for i, (cid, _) in enumerate(m["centers"])}
        self.assertEqual(row[2][reps.index("perf")], "f")
        self.assertEqual(row[3][reps.index("gym")], "s")
        self.assertEqual(row[4][reps.index("sales")], "e")
        self.assertEqual(row[1][reps.index("sales")], "o")
        self.assertEqual(m["errors"][f"2|perf"], "HTTP 500")
        self.assertEqual(m["rows"][0][reps.index("sales")], 1)

    def test_estimate_uses_measured_timings(self):
        SectionResult.objects.filter(session=self.session, report="sales").update(seconds=10)
        e = self.get("api/estimate/", list="test", only="sales", workers=2).json()
        self.assertEqual((e["centers"], e["reports"], e["pairs"]), (4, 1, 4))
        self.assertEqual(e["seconds_low"], round((10 + services.PAIR_OVERHEAD) * 4 / 2))
        self.assertIn("sales", e["measured"])
        SectionResult.objects.filter(report="perf_ratings").update(seconds=None)  # no timings recorded yet
        e2 = self.get("api/estimate/", list="test", only="perf_ratings", workers=1).json()
        self.assertIn("perf_ratings", e2["assumed"])
        self.assertEqual(self.get("api/estimate/", list="nope").status_code, 400)

    def test_compare_two_sessions(self):
        other = engine.run_session(engine.new_session("2026-08-01", "2026-08-31", [1, 2], ["sales", "perf"]))
        c = self.get("api/compare/", a=self.session.session_id, b=other.session_id).json()
        rows = {r["center_id"]: r for r in c["rows"]}
        self.assertEqual(rows[1]["diff"]["sales"], 0)
        self.assertIsNone(rows[3]["b"])  # center 3 is only in the first session
        self.assertEqual(self.get("api/compare/", a="zzz", b="yyy").status_code, 404)

    # ------------------------------------------------------------------ login / run
    def test_login_flow(self):
        self.assertTrue(self.post("api/login/send/").json()["ok"])
        self.assertEqual(self.post("api/login/verify/", {"otp": "abc"}).status_code, 400)
        self.assertFalse(self.post("api/login/verify/", {"otp": "000000"}).json()["ok"])
        self.assertTrue(self.post("api/login/verify/", {"otp": "123456"}).json()["ok"])

    def test_run_requires_login_and_validates(self):
        body = {"start": "2026-09-01", "end": "2026-09-30", "list": "test", "workers": 2}
        self.logged_in = False
        self.assertEqual(self.post("api/run/", body).status_code, 401)
        self.logged_in = True
        self.assertEqual(self.post("api/run/", {**body, "start": "2026-13-01"}).status_code, 400)
        self.assertEqual(self.post("api/run/", {**body, "workers": 9}).status_code, 400)
        self.assertEqual(self.post("api/run/", {**body, "only": ["nope"]}).status_code, 400)

    def test_run_stop_pause_cycle(self):
        body = {"start": "2026-09-01", "end": "2026-09-30", "list": "test", "workers": 2, "only": ["sales"]}
        with mock.patch.object(jobs, "async_task") as queued:
            r = self.post("api/run/", body)
            self.assertEqual(r.status_code, 200)
            queued.assert_called_once()
            self.assertEqual(self.post("api/run/", body).status_code, 409)  # one job at a time
            job = Job.objects.get(pk=r.json()["job"])
            self.assertEqual(job.state, Job.QUEUED)
            Job.objects.filter(pk=job.pk).update(state=Job.RUNNING, heartbeat=timezone.now())
            self.assertTrue(self.post("api/pause/", {"on": True}).json()["paused"])
            self.assertTrue(self.get("api/status/").json()["job"]["paused"])
            self.post("api/pause/", {"on": False})
            self.post("api/stop/")
            self.assertTrue(Job.objects.get(pk=job.pk).stop_requested)
            Job.objects.filter(pk=job.pk).update(state=Job.DONE)
            self.assertEqual(self.post("api/pause/", {"on": True}).status_code, 409)

    # ------------------------------------------------------------------ centers
    def test_centers_list_and_sync(self):
        data = self.get("api/centers/").json()
        self.assertEqual(len(data["centers"]), 4)
        r = self.post("api/centers/list/", {"list": "test", "ids": [1, 2, 3]})
        self.assertEqual(r.json()["count"], 3)
        self.assertEqual(center_services.list_ids("test"), [1, 2, 3])
        self.assertEqual(self.post("api/centers/list/", {"list": "test", "ids": [999]}).status_code, 400)
        self.assertEqual(self.post("api/centers/list/", {"list": "Bad Name!", "ids": []}).status_code, 400)
        diff = {"added": [], "removed": [], "state_changed": [], "renamed": [], "total": 4, "active": 4}
        with mock.patch.object(services.sync, "sync_from_kronos", lambda: diff):
            self.assertEqual(self.post("api/centers/sync/").json()["total"], 4)
        self.logged_in = False
        self.assertEqual(self.post("api/centers/sync/").status_code, 401)

    # ------------------------------------------------------------------ safety
    def test_download_is_confined_to_the_session_folder(self):
        sid = self.session.session_id
        ok = self.get("download/", session=sid, path="merged/sales.csv")
        self.assertEqual(ok.status_code, 200)
        self.assertIn("attachment", ok["Content-Disposition"])
        for evil in ("../../db.sqlite3", "..\\..\\db.sqlite3", "/etc/passwd", "centers"):
            self.assertEqual(self.get("download/", session=sid, path=evil).status_code, 404, evil)
        self.assertEqual(self.get("download/", session="nope", path="x").status_code, 404)

    def test_post_needs_csrf_and_host_is_checked(self):
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(self.post("api/stop/", client=strict).status_code, 403)
        quiet = Client(raise_request_exception=False)
        self.assertEqual(quiet.get(BASE + "api/status/", HTTP_HOST="evil.com").status_code, 400)

    def test_get_endpoints_reject_post_and_vice_versa(self):
        self.assertEqual(self.client.post(BASE + "api/status/").status_code, 405)
        self.assertEqual(self.client.get(BASE + "api/run/").status_code, 405)
