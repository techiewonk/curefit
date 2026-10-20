"""Scraper settings. The values live in django-constance, so they are editable in the admin
(System → Settings); defaults are declared in config/settings.py (CONSTANCE_CONFIG)."""
import random
import time
from dataclasses import dataclass

from constance import config

SPEED_PROFILES = {"gentle": 1, "normal": 2, "fast": 4}


@dataclass(frozen=True)
class RunSettings:
    """A snapshot taken when a run starts, so one run never sees settings change under it."""

    delay_min: float
    delay_max: float
    max_retries: int
    backoff_base: float
    row_cap: int
    poll_seconds: int
    request_timeout: int
    auth_fail_limit: int
    report_fail_limit: int
    max_workers: int

    @classmethod
    def load(cls):
        return cls(config.DELAY_MIN, config.DELAY_MAX, config.MAX_RETRIES, config.BACKOFF_BASE,
                   config.ROW_CAP, config.POLL_SECONDS, config.REQUEST_TIMEOUT,
                   config.AUTH_FAIL_LIMIT, config.REPORT_FAIL_LIMIT, config.MAX_WORKERS)

    def pause(self):
        """The polite delay between two requests."""
        time.sleep(random.uniform(self.delay_min, self.delay_max))
