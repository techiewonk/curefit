"""Scraper tunables. Values can be overridden from the admin (AppSetting rows)."""
import random
import time

DEFAULTS = {
    "delay_min": 1.0,          # min seconds between requests
    "delay_max": 3.0,          # max seconds between requests
    "max_retries": 4,          # retries per failed request
    "backoff_base": 2.0,       # exponential back-off base (seconds)
    "row_cap": 10000,          # Metabase's default cap; at or above this a result may be cut off
    "poll_seconds": 240,       # how long to wait for a slow Metabase query
    "request_timeout": 300,    # per-request read timeout (seconds)
    "auth_fail_limit": 3,      # consecutive 401/403s before the run stops and asks for a re-login
    "report_fail_limit": 3,    # failures in a row (and no success) before a report is skipped
    "max_workers": 4,          # most centers fetched at the same time
}

SPEED_PROFILES = {"gentle": 1, "normal": 2, "fast": 4}


def get(key):
    """A setting: the admin's override if there is one, else the default."""
    from scraper.models import AppSetting  # imported late: this module loads before the apps
    row = AppSetting.objects.filter(key=key).first()
    return DEFAULTS[key] if row is None or row.value is None else type(DEFAULTS[key])(row.value)


def polite_sleep(lo=None, hi=None):
    time.sleep(random.uniform(lo if lo is not None else DEFAULTS["delay_min"],
                              hi if hi is not None else DEFAULTS["delay_max"]))
