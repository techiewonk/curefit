"""Shared test helper: fakes the network so engine and job tests run offline."""
import tempfile
from unittest import mock

from centers.models import Center
from scraper import conf, engine, files, kronos
from scraper.reports import REPORTS


class FakeKronosMixin:
    """Centers 1..4; `sales` returns `center_id` rows. Toggle behaviour through self.mode."""

    def setUp(self):
        super().setUp()
        for i in range(1, 5):
            Center.objects.create(center_id=i, name=f"Cult {i}", code=f"C{i}", state="ACTIVE")
        self.tmp = tempfile.mkdtemp()
        self.calls, self.mode = [], {}
        self.by_card = {v["card"]: k for k, v in REPORTS.items()}
        for p in (
            mock.patch.object(files, "RUNS_DIR", self.tmp),
            mock.patch.object(conf.RunSettings, "pause", lambda self: None),
            mock.patch.object(engine.C, "load_session", lambda: (object(), {})),
            mock.patch.object(kronos, "get_token",
                              lambda http, rep, cid, s, e, cfg=None: f"{rep['dashboard']}:{cid}"),
            mock.patch.object(kronos, "warm_up", lambda mb, token, cfg=None: None),
            mock.patch.object(kronos, "get_rows", self.fake_rows),
        ):
            p.start()
            self.addCleanup(p.stop)

    def fake_rows(self, mb, rep, token, start, end, cfg=None):
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
            return [[cid, "a", "p", "pk", "d", "Yes", "Yes", 10]] * self.mode.get("sales_rows", cid), []
        if name == "perf":
            return [["2026-07-01", "1. Total Revenue", 100.0], ["2026-07-01", "1.a New Revenue", 60.0]], []
        if name == "gym":
            return [["u", "n", "pk", 5, "d", "d", "d", "Active", "Online", "New", "Adv", "d"]], []
        return [[cid, 7]], ["center", "value"]
