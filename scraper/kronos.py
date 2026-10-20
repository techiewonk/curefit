"""Talking to Kronos and Metabase (ported from scrape_sales.py, behaviour unchanged except SlowQuery)."""
import json
import logging
import random
import time
import urllib.parse
import uuid

import requests

import curefit_login as C

from . import conf

log = logging.getLogger("scraper.http")


class AuthError(Exception):
    """Session rejected (401/403): retrying is pointless, a re-login is needed."""


class SlowQuery(RuntimeError):
    """Metabase did not finish within the poll window. Not retried blindly: the engine decides."""


def with_retry(fn, what, max_retries=None, backoff=None):
    max_retries = max_retries or conf.DEFAULTS["max_retries"]
    backoff = backoff or conf.DEFAULTS["backoff_base"]
    last = None
    for attempt in range(max_retries):
        try:
            return fn()
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            if code in (401, 403):
                raise AuthError(f"{what}: HTTP {code}") from e
            if code in (400, 404):
                raise
            last = e
        except SlowQuery:
            raise
        except Exception as e:
            last = e
        wait = backoff * (2 ** attempt) + random.uniform(0, 1)
        log.warning("retry %s: %s (attempt %d/%d, waiting %.1fs)", what, last, attempt + 1,
                    max_retries, wait)
        time.sleep(wait)
    raise last


def get_token(http, rep, center_id, start, end, timeout=None):
    """Signed Metabase token for one center and dashboard (one Kronos call)."""
    timeout = timeout or conf.DEFAULTS["request_timeout"]

    def do():
        payload = {
            "payload": {"resource": {"dashboard": rep["dashboard"]},
                        "params": {"center_id": str(center_id)}},
            "query": rep["iframe_query"](center_id, start, end),
            "fragmentIdentifier": "bordered=true&titled=true",
        }
        r = http.post("https://kronos.curefit.co/v1/metabase/iframeUrl", json=payload,
                      headers={"referer": rep["referer"]}, timeout=timeout)
        r.raise_for_status()
        return r.json()["iframeUrl"].split("/dashboard/")[1].split("?")[0]

    return with_retry(do, "iframeUrl")


def embed_headers(token):
    return {
        "accept": "application/json",
        "x-metabase-client": "embedding-iframe",
        "x-metabase-embedded": "true",
        "referer": f"https://metabase.curefit.co/embed/dashboard/{token}",
        "user-agent": C.UA,
    }


def warm_up(mb, token, timeout=None):
    """Mimic the browser's dashboard load (session properties + dashboard definition).

    Best effort: a failure here is logged but never blocks the data call.
    """
    timeout = timeout or conf.DEFAULTS["request_timeout"]
    hdr = embed_headers(token)
    for url in ("https://metabase.curefit.co/api/session/properties",
                f"https://metabase.curefit.co/api/embed/dashboard/{token}"
                f"?dashboard_load_id={uuid.uuid4()}"):
        try:
            mb.get(url, headers=hdr, timeout=timeout).raise_for_status()
        except Exception as e:
            log.warning("warm-up failed %s: %s", url.split("?")[0], e)


def get_rows(mb, rep, token, start, end, poll_seconds=None, timeout=None):
    """Return (rows, column_names) once Metabase reports the query completed.

    A 200 or a 202 body can already be the finished result; anything else is polled until
    `poll_seconds`, then SlowQuery is raised.
    """
    poll_seconds = poll_seconds or conf.DEFAULTS["poll_seconds"]
    timeout = timeout or conf.DEFAULTS["request_timeout"]
    params = urllib.parse.quote(json.dumps(rep["data_params"](start, end)))
    url = (f"https://metabase.curefit.co/api/embed/dashboard/{token}"
           f"/dashcard/{rep['dashcard']}/card/{rep['card']}?parameters={params}")
    hdr = {**embed_headers(token), "x-metabase-guest-embed": "true"}

    def do():
        deadline = time.time() + poll_seconds
        while True:
            r = mb.get(url, headers=hdr, timeout=timeout)
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
                raise SlowQuery(f"metabase query not completed after {poll_seconds}s "
                                f"(status={status!r})")
            time.sleep(2)

    return with_retry(do, "data")
