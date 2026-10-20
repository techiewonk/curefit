"""Talking to Kronos and Metabase (ported from the old script; behaviour unchanged except SlowQuery)."""
import json
import logging
import random
import re
import time
import urllib.parse
import uuid

import requests

from . import login
from .throttle import Stopped  # noqa: F401 - raised from here too (a stop while polling)

log = logging.getLogger("scraper.http")
_TOKEN_IN_URL = re.compile(r"/dashboard/[^/?\s]+")
BUSY_CODES = (429, 500, 502, 503, 504)  # the server is overloaded: tell the throttle
POLL_FIRST, POLL_GROWTH, POLL_MAX = 2.0, 1.5, 15.0  # seconds between two looks at an unfinished query


def scrub(text):
    """The signed embed token is a credential: keep it out of logs, the job log, the database and the pages."""
    return _TOKEN_IN_URL.sub("/dashboard/<token>", str(text))


class AuthError(Exception):
    """Session rejected (401/403): retrying is pointless, a re-login is needed."""


class SlowQuery(RuntimeError):
    """Metabase did not finish within the poll window. Not retried blindly: the engine decides."""


def _sleep(seconds, should_stop=None):
    """Sleep, but give up (Stopped) within a second of the run being stopped."""
    end = time.monotonic() + seconds
    while True:
        if should_stop is not None and should_stop():
            raise Stopped("stopped while waiting")
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(1.0, left))


def with_retry(fn, what, cfg, should_stop=None, on_busy=None):
    """Call fn, retrying network errors and 5xx with a growing wait.

    should_stop: a Stop pressed during a wait ends the call (Stopped). on_busy: called on 429/5xx, so the
    throttle can slow the whole run down."""
    last = None
    for attempt in range(cfg.max_retries):
        try:
            return fn()
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            if code in (401, 403):
                raise AuthError(f"{what}: HTTP {code}") from e
            if code in (400, 404):
                raise
            if code in BUSY_CODES and on_busy is not None:
                on_busy()
            last = e
        except (SlowQuery, Stopped):
            raise
        except Exception as e:
            last = e
        if attempt + 1 >= cfg.max_retries:
            break  # that was the last try: do not wait before giving up
        wait = cfg.backoff_base * (2 ** attempt) + random.uniform(0, 1)
        log.warning("retry %s: %s (attempt %d/%d, waiting %.1fs)", what, scrub(last), attempt + 1,
                    cfg.max_retries, wait)
        _sleep(wait, should_stop)
    raise last or RuntimeError(f"{what}: no attempt was made (MAX_RETRIES is {cfg.max_retries})")


def get_token(http, rep, center_id, start, end, cfg):
    """Signed Metabase token for one center and dashboard (one Kronos call)."""

    def do():
        payload = {
            "payload": {"resource": {"dashboard": rep["dashboard"]},
                        "params": {"center_id": str(center_id)}},
            "query": rep["iframe_query"](center_id, start, end),
            "fragmentIdentifier": "bordered=true&titled=true",
        }
        r = http.post("https://kronos.curefit.co/v1/metabase/iframeUrl", json=payload,
                      headers={"referer": rep["referer"]}, timeout=cfg.request_timeout)
        r.raise_for_status()
        return r.json()["iframeUrl"].split("/dashboard/")[1].split("?")[0]

    return with_retry(do, "iframeUrl", cfg)


def embed_headers(token):
    return {
        "accept": "application/json",
        "x-metabase-client": "embedding-iframe",
        "x-metabase-embedded": "true",
        "referer": f"https://metabase.curefit.co/embed/dashboard/{token}",
        "user-agent": login.UA,
    }


def warm_up(mb, token, cfg):
    """Mimic the browser's dashboard load (session properties + dashboard definition).

    Best effort: a failure here is logged but never blocks the data call.
    """
    hdr = embed_headers(token)
    for url in ("https://metabase.curefit.co/api/session/properties",
                f"https://metabase.curefit.co/api/embed/dashboard/{token}"
                f"?dashboard_load_id={uuid.uuid4()}"):
        try:
            mb.get(url, headers=hdr, timeout=cfg.request_timeout).raise_for_status()
        except Exception as e:
            log.warning("warm-up failed %s: %s", scrub(url.split("?")[0]), scrub(e))


def get_rows(mb, rep, token, start, end, cfg, granularity="month", should_stop=None, on_busy=None):
    """Return (rows, column_names) once Metabase reports the query completed.

    A 200 or a 202 body can already be the finished result; anything else is polled (2s, then a little longer
    each time, at most 15s apart) until `cfg.poll_seconds` after the first request, then SlowQuery is raised.
    A retry after a network error does not restart that clock. should_stop / on_busy: see with_retry.
    """
    params = urllib.parse.quote(json.dumps(rep["data_params"](start, end, granularity)))
    url = (f"https://metabase.curefit.co/api/embed/dashboard/{token}"
           f"/dashcard/{rep['dashcard']}/card/{rep['card']}?parameters={params}")
    hdr = {**embed_headers(token), "x-metabase-guest-embed": "true"}
    deadline = time.time() + cfg.poll_seconds

    def do():
        wait = POLL_FIRST
        while True:
            r = mb.get(url, headers=hdr, timeout=cfg.request_timeout)
            r.raise_for_status()
            try:
                j = r.json()
            except ValueError:
                j = {}
            status = j.get("status") if isinstance(j, dict) else None
            if status == "completed":
                data = j["data"]
                return data["rows"], [c.get("name") for c in data.get("cols", [])]
            if status == "failed":
                raise RuntimeError(f"metabase query failed: {j.get('error')}")
            if time.time() > deadline:
                raise SlowQuery(f"metabase query not completed after {cfg.poll_seconds}s "
                                f"(status={status!r})")
            _sleep(min(wait, max(0.0, deadline - time.time()) + 0.1), should_stop)
            wait = min(POLL_MAX, wait * POLL_GROWTH)

    return with_retry(do, "data", cfg, should_stop, on_busy)
