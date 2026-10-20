"""Kronos login, the login check, Metabase polling and the request gate (G4, G25, G52, G12)."""
import itertools
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest import TestCase, mock

import requests

from scraper import auth, kronos, login
from scraper.throttle import Gate, Stopped


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class FakeHttp:
    """Stands in for requests.Session: answers in order, records nothing secret."""

    def __init__(self, posts=(), gets=()):
        self.cookies = requests.cookies.RequestsCookieJar()
        self.headers = {}
        self.posts, self.gets = list(posts), list(gets)

    def _next(self, queue):
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def post(self, url, **kw):
        if "send" in url:
            self.cookies.set("accounts_sid", "pending-cookie")
        return self._next(self.posts)

    def get(self, url, **kw):
        if "erpToken=" in url:
            self.cookies.set("kronos_sid", "new-kronos-cookie")
        return self._next(self.gets)


class LoginFileTests(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.saved = self.dir / "session.json"
        self.saved.write_text(json.dumps({"cookies": {"kronos_sid": "old"}, "extra": {"erpToken": "OLD"}}))
        for p in (mock.patch.object(login, "SESSION_FILE", self.saved),
                  mock.patch.object(login, "PENDING_FILE", self.dir / "session.pending.json"),
                  mock.patch("scraper.auth.config", mock.Mock(LOGIN_EMAIL="me@example.com"))):
            p.start()
            self.addCleanup(p.stop)
        auth._cache.update(at=0.0, state=auth.REJECTED)

    def old_login_is_intact(self):
        return json.loads(self.saved.read_text())["extra"]["erpToken"] == "OLD"

    def use(self, http):
        return mock.patch.object(login, "new_session", lambda: http)

    def test_sending_an_otp_keeps_the_working_login(self):
        http = FakeHttp(posts=[FakeResponse(200, {})])
        with self.use(http):
            self.assertTrue(auth.send_otp())
        self.assertTrue(self.old_login_is_intact())
        self.assertTrue((self.dir / "session.pending.json").exists())

    def test_a_wrong_otp_keeps_the_working_login(self):
        http = FakeHttp(posts=[FakeResponse(200, {}), FakeResponse(401, {"message": "invalid otp"})])
        with self.use(http):
            auth.send_otp()
            result = auth.verify_otp("000000")
        self.assertFalse(result)
        self.assertIn("not accepted", result.message)
        self.assertTrue(self.old_login_is_intact())

    def test_a_body_that_is_not_a_dict_or_not_json_is_no_crash(self):
        for body in (["token"], ValueError("not json"), None):
            http = FakeHttp(posts=[FakeResponse(200, body)])
            with self.use(http):
                self.assertFalse(auth.verify_otp("123456"))
        self.assertTrue(self.old_login_is_intact())

    def test_a_token_from_the_redirect_header_logs_in_and_replaces_the_login(self):
        http = FakeHttp(posts=[FakeResponse(200, {}), FakeResponse(302, ValueError(), {"Location": "/x?erpToken=NEW&a=1"})],
                        gets=[FakeResponse(200), FakeResponse(200)])  # open the Kronos session, then the check
        with self.use(http):
            auth.send_otp()
            result = auth.verify_otp("123456")
        self.assertTrue(result, result.message)
        saved = json.loads(self.saved.read_text())
        self.assertEqual(saved["extra"]["erpToken"], "NEW")
        self.assertIn("kronos_sid", saved["cookies"])
        self.assertFalse((self.dir / "session.pending.json").exists())
        self.assertTrue(auth.logged_in())

    def test_network_errors_are_messages_not_crashes(self):
        http = FakeHttp(posts=[requests.ConnectionError("offline"), requests.Timeout("slow")])
        with self.use(http):
            sent = auth.send_otp()
            checked = auth.verify_otp("123456")
        self.assertFalse(sent)
        self.assertIn("could not be reached", sent.message)
        self.assertFalse(checked)
        self.assertTrue(self.old_login_is_intact())

    def test_a_damaged_session_file_means_not_logged_in(self):
        self.saved.write_text('{"cookies": {"a": ')  # cut off half way
        http, extra = login.load_session()
        self.assertEqual(extra, {})
        self.assertEqual(login.session_state(), login.REJECTED)
        self.saved.write_text("[1, 2]")
        self.assertEqual(login.load_session()[1], {})

    def test_a_save_that_fails_half_way_leaves_the_old_file_and_no_temp_file(self):
        with mock.patch("scraper.login.json.dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                login.save_session(FakeHttp(), {"erpToken": "NEW"})
        self.assertTrue(self.old_login_is_intact())
        self.assertEqual([p.name for p in self.dir.iterdir()], ["session.json"])

    def test_a_briefly_locked_file_is_replaced_on_a_second_try(self):
        real, tries = os.replace, []

        def locked_once(a, b):
            tries.append(1)
            if len(tries) == 1:
                raise PermissionError("in use")
            return real(a, b)
        with mock.patch("scraper.login.os.replace", locked_once), mock.patch("scraper.login.time.sleep"):
            login.save_session(FakeHttp(), {"erpToken": "NEW"})
        self.assertEqual(len(tries), 2)
        self.assertFalse(self.old_login_is_intact())


class LoginCheckTests(TestCase):
    """G25: the check is short, says why, and is not repeated on every page."""

    def setUp(self):
        auth._cache.update(at=0.0, state=auth.REJECTED)
        self.addCleanup(auth._cache.update, at=0.0, state=auth.REJECTED)  # later tests must not see "unreachable"

    def state_for(self, answer):
        http = FakeHttp(gets=[answer])
        return login.session_state(http, {"erpToken": "T"})

    def test_three_answers(self):
        self.assertEqual(self.state_for(FakeResponse(200)), login.OK)
        self.assertEqual(self.state_for(FakeResponse(401)), login.REJECTED)
        self.assertEqual(self.state_for(FakeResponse(503)), login.UNREACHABLE)
        self.assertEqual(self.state_for(requests.ConnectionError("offline")), login.UNREACHABLE)
        self.assertEqual(login.session_state(FakeHttp(), {}), login.REJECTED)  # no login at all

    def test_the_probe_is_short(self):
        seen = {}
        http = FakeHttp()
        http.get = lambda url, **kw: seen.update(kw) or FakeResponse(200)
        login.session_state(http, {"erpToken": "T"})
        self.assertLessEqual(seen["timeout"], 5)

    def test_answers_are_cached_and_failures_longer(self):
        calls = []
        with mock.patch.object(login, "session_state", lambda: calls.append(1) or login.UNREACHABLE):
            self.assertEqual(auth.state(), auth.UNREACHABLE)
            self.assertFalse(auth.logged_in())
            auth._cache["at"] = time.time() - auth.CACHE_SECONDS - 1  # a working login would be asked again now
            self.assertFalse(auth.logged_in())
            self.assertEqual(len(calls), 1)
            self.assertFalse(auth.logged_in(fresh=True))  # fresh: asked again
            self.assertEqual(len(calls), 2)
            self.assertFalse(auth.logged_in(fresh=True))  # but not twice within a few seconds
            self.assertEqual(len(calls), 2)


class PollingTests(TestCase):
    """G52: polling grows, honours Stop, and tells the throttle about an overloaded server."""

    cfg = mock.Mock(poll_seconds=240, request_timeout=5, max_retries=3, backoff_base=0)
    rep = {"data_params": lambda s, e, g: {}, "dashcard": 1, "card": 2}

    def test_polls_slower_and_slower(self):
        waits = []
        http = FakeHttp(gets=[FakeResponse(202, {"status": "running"})] * 5 +
                        [FakeResponse(200, {"status": "completed", "data": {"rows": [[1]], "cols": [{"name": "a"}]}})])
        with mock.patch.object(kronos, "_sleep", lambda s, stop=None: waits.append(s)):
            rows, cols = kronos.get_rows(http, self.rep, "tok", "2026-07-01", "2026-07-31", self.cfg)
        self.assertEqual((rows, cols), ([[1]], ["a"]))
        self.assertEqual(waits[0], 2.0)
        self.assertEqual(waits, sorted(waits))
        self.assertGreater(waits[-1], waits[0])

    def test_stop_ends_the_wait_for_metabase(self):
        http = FakeHttp(gets=[FakeResponse(202, {"status": "running"})] * 10)
        began = time.monotonic()
        with self.assertRaises(Stopped):
            kronos.get_rows(http, self.rep, "tok", "2026-07-01", "2026-07-31", self.cfg, should_stop=lambda: True)
        self.assertLess(time.monotonic() - began, 2)

    def test_an_overloaded_server_slows_the_run_down(self):
        busy = []
        http = FakeHttp(gets=[FakeResponse(429), FakeResponse(503),
                              FakeResponse(200, {"status": "completed", "data": {"rows": [], "cols": []}})])
        with mock.patch.object(kronos, "_sleep", lambda s, stop=None: None):
            kronos.get_rows(http, self.rep, "tok", "a", "b", self.cfg, on_busy=lambda: busy.append(1))
        self.assertEqual(len(busy), 2)

    def test_a_retry_does_not_restart_the_poll_clock(self):
        cfg = mock.Mock(poll_seconds=2, request_timeout=5, max_retries=3, backoff_base=0)
        http = FakeHttp(gets=[requests.ConnectionError("blip"), FakeResponse(202, {"status": "running"})])
        clock = itertools.chain([100.0], itertools.count(103.0))  # the clock starts at the first request, not the retry
        with mock.patch.object(kronos, "_sleep", lambda s, stop=None: None), \
                mock.patch("scraper.kronos.time.time", lambda: next(clock)):
            with self.assertRaises(kronos.SlowQuery):
                kronos.get_rows(http, self.rep, "tok", "a", "b", cfg)


class GateStopTests(TestCase):
    def test_a_request_waiting_for_a_slot_gives_up_on_stop(self):
        gate, stop, out = Gate(1, 1), threading.Event(), {}
        with gate.slot():
            def wait():
                try:
                    with gate.slot(stop.is_set):
                        out["got"] = True
                except Stopped:
                    out["stopped"] = True
            t = threading.Thread(target=wait)
            t.start()
            time.sleep(0.2)
            stop.set()
            t.join(3)
        self.assertEqual(out, {"stopped": True})
