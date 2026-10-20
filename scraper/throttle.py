"""How many requests Curefit's servers get at the same time.

In adaptive mode the limit halves when the servers struggle (a timeout or an error) and creeps up
again while answers stay fast, never above `maximum`.
"""
import threading
import time
from contextlib import contextmanager

FAST_SECONDS = 30   # an answer faster than this counts as healthy
GROW_AFTER = 8      # healthy answers in a row before one more request is allowed
COOLDOWN = 30       # seconds between two reductions


class Stopped(Exception):
    """The run was stopped while this request waited (for a slot or for Metabase): nothing was fetched."""


class Gate:
    def __init__(self, start, maximum, adaptive=False):
        self.limit, self.maximum, self.adaptive = start, maximum, adaptive
        self._cond = threading.Condition()
        self._active = self._streak = 0
        self._last_cut = float("-inf")

    @contextmanager
    def slot(self, should_stop=None):
        """Hold one of the `limit` request slots while talking to Curefit.

        should_stop: checked every second while waiting; when it says yes, Stopped is raised instead of waiting on."""
        with self._cond:
            while self._active >= self.limit:
                self._cond.wait(timeout=1)
                if should_stop is not None and should_stop():
                    raise Stopped("stopped while waiting for a free request slot")
            self._active += 1
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    def healthy(self, seconds):
        if not self.adaptive:
            return
        with self._cond:
            if seconds > FAST_SECONDS:
                self._streak = 0
                return
            self._streak += 1
            if self._streak >= GROW_AFTER and self.limit < self.maximum:
                self.limit += 1
                self._streak = 0
                self._cond.notify_all()

    def struggling(self):
        if not self.adaptive:
            return
        with self._cond:
            self._streak = 0
            if self.limit > 1 and time.monotonic() - self._last_cut > COOLDOWN:
                self.limit = max(1, self.limit // 2)
                self._last_cut = time.monotonic()
