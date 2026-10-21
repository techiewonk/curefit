"""Shared test helper: fakes the network so engine and job tests run offline."""
import tempfile
from unittest import mock

from centers.models import Center
from scraper import auth, conf, files, kronos, login
from scraper.reports import REPORTS


class FakeKronosMixin:
    """Centers 1..4; `sales` returns `center_id` rows. Toggle behaviour through self.mode."""

    def setUp(self):
        super().setUp()
        for i in range(1, 5):
            Center.objects.create(center_id=i, name=f"Cult {i}", code=f"C{i}", state="ACTIVE")
        self.tmp = tempfile.mkdtemp()
        self.calls, self.mode, self.params = [], {}, []
        self.by_card = {v["card"]: k for k, v in REPORTS.items()}
        for p in (
            mock.patch.object(auth, "logged_in", lambda fresh=False: True),  # jobs.enqueue checks the login
            mock.patch.object(files, "RUNS_DIR", self.tmp),
            mock.patch.object(conf.RunSettings, "pause", lambda self: None),
            mock.patch.object(login, "load_session", lambda: (object(), {})),
            mock.patch.object(kronos, "get_token",
                              lambda http, rep, cid, s, e, cfg=None, **kw: f"{rep['dashboard']}:{cid}"),
            mock.patch.object(kronos, "warm_up", lambda mb, token, cfg=None: None),
            mock.patch.object(kronos, "get_rows", self.fake_rows),
        ):
            p.start()
            self.addCleanup(p.stop)

    def fake_rows(self, mb, rep, token, start, end, cfg=None, granularity="month", **kw):  # kw: should_stop, on_busy
        cid = int(token.split(":")[1])
        name = self.by_card[rep["card"]]
        self.calls.append((name, cid))
        self.params.append(rep["data_params"](start, end, granularity))
        hook = self.mode.get("hook")
        if hook:
            hook(name, cid)
        if name in self.mode.get("fail", ()):
            raise RuntimeError("HTTP 500")
        if name in self.mode.get("leak", ()):  # an error that carries the signed token in its text
            raise RuntimeError(f"500 for https://metabase.curefit.co/api/embed/dashboard/{token}/dashcard/1")
        once = self.mode.setdefault("empty_once_seen", set())
        if (name, cid, start) in self.mode.get("empty_once", ()) and (name, cid, start) not in once:
            once.add((name, cid, start))  # Metabase answering empty by mistake: the retry gets the rows
            return [], []
        if name in self.mode.get("slow", ()):
            raise kronos.SlowQuery("not completed")
        if self.mode.get("auth"):
            raise kronos.AuthError("HTTP 401")
        if name == "sales" and (self.mode.get("empty_all") or (cid == 2 and self.mode.get("empty2"))):
            return [], []
        if name == "sales":
            return [[cid, "a", "p", "pk", "d", "Yes", "Yes", 10]] * self.mode.get("sales_rows", cid), []
        if name == "perf":
            return [["2026-07-01", "1. Total Revenue", 100.0], ["2026-07-01", "1.a New Revenue", 60.0]], []
        if name == "gym":
            return [["u", "n", "pk", 5, "d", "d", "d", "Active", "Online", "New", "Adv", "d"]], []
        return [[cid, 7]], ["center", "value"]
