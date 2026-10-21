from unittest import mock

import requests
from django.test import TestCase
from django.urls import reverse

from centers.models import Center, CenterList, KnownEmpty
from scraper import kronos
from scraper.conf import RunSettings
from scraper.throttle import Stopped


class TokenRetryHonoursStopTests(TestCase):
    def test_stop_pressed_while_the_token_request_waits_to_retry(self):
        class Http:
            def post(self, *a, **kw):
                raise requests.ConnectionError("down")
        cfg = mock.Mock(spec=RunSettings, max_retries=3, backoff_base=30, request_timeout=5)
        busy = []
        rep = {"dashboard": 1, "iframe_query": lambda c, s, e: {}, "referer": "x"}
        with mock.patch.object(kronos.time, "sleep") as slept:
            with self.assertRaises(Stopped):
                kronos.get_token(Http(), rep, 1, "2026-01-01", "2026-01-31", cfg,
                                 should_stop=lambda: True, on_busy=lambda: busy.append(1))
        slept.assert_not_called()  # it gave up at once instead of waiting 30 s


class MissingDaysTotalTests(TestCase):
    def test_the_total_leaves_out_known_empty_gyms(self):
        for pk in (1, 2, 3):
            Center.objects.update_or_create(pk=pk, defaults={"name": f"Gym {pk}"})
        lst = CenterList.objects.update_or_create(name="actual")[0]
        lst.centers.set([1, 2, 3])
        KnownEmpty.objects.create(center_id=3, report="sales")
        r = self.client.get(reverse("console:coverage") + "?list=actual", headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["total"], 2)
        self.assertEqual(r.context["known_empty"], 1)
        self.assertContains(r, "1 known-empty gym left out")
