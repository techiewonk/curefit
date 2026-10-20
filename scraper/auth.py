"""Kronos login (the emailed OTP) as a small service over `scraper.login`."""
import logging
import threading
import time

import requests
from constance import config

from . import login

log = logging.getLogger("scraper.auth")

CACHE_SECONDS = 30      # a working login is checked again after this long
FAIL_CACHE_SECONDS = 120  # "not logged in" / "Kronos unreachable" is asked again less often (the check can take 5s)
FRESH_SECONDS = 3       # fresh=True right after another check reuses that answer instead of asking twice
OK, REJECTED, UNREACHABLE = login.OK, login.REJECTED, login.UNREACHABLE
_cache = {"at": 0.0, "state": REJECTED}
_lock = threading.Lock()


class Result:
    """The outcome of a login step: true or false like a bool, plus a message for the page."""

    def __init__(self, ok, message):
        self.ok, self.message = bool(ok), message

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return f"Result({self.ok}, {self.message!r})"


def state(fresh=False):
    """OK, REJECTED (no login, or Kronos refuses it) or UNREACHABLE (offline / Kronos down). Cached briefly."""
    with _lock:
        age = time.time() - _cache["at"]
        keep = FRESH_SECONDS if fresh else (CACHE_SECONDS if _cache["state"] == OK else FAIL_CACHE_SECONDS)
        if age <= keep:
            return _cache["state"]
    answer = login.session_state()  # outside the lock: a slow check must not hold every other page
    with _lock:
        _cache.update(at=time.time(), state=answer)
    return answer


def logged_in(fresh=False):
    """True if Kronos accepts the saved session (see state() for why not)."""
    return state(fresh) == OK


def last_state():
    """The answer of the last check, without asking Kronos again (for the message after logged_in() said no)."""
    with _lock:
        return _cache["state"]


def not_logged_in_message(action="start a scrape"):
    """Why a run cannot start, in words: Kronos refused the login, or Kronos could not be reached at all."""
    if last_state() == UNREACHABLE:
        return f"Kronos could not be reached (offline, or Kronos is down) - check the connection, then {action} again."
    return "Not logged in to Kronos - send and verify the OTP on the Run page first."


def _remember(answer):
    with _lock:
        _cache.update(at=time.time(), state=answer)


def send_otp():
    """Ask Kronos to email an OTP to the configured account. A Result: true when it was sent.

    The current login is kept until a new one is verified."""
    try:
        response = login.send_otp(config.LOGIN_EMAIL)
    except requests.RequestException as e:
        log.warning("otp send failed: %s", e.__class__.__name__)
        return Result(False, "Kronos could not be reached - check the internet connection and try again.")
    except OSError as e:  # the login in progress could not be kept on disk
        log.warning("otp send: could not keep the pending login: %s", e.__class__.__name__)
        return Result(False, "The OTP was sent, but the login in progress could not be saved next to the app "
                             "(is the folder read-only?). Fix that, then send a new OTP.")
    if response.status_code != 200:
        return Result(False, f"Kronos did not send the OTP (HTTP {response.status_code}). "
                             "Check the login email in Settings.")
    return Result(True, f"OTP sent to {config.LOGIN_EMAIL} - check the inbox.")


def verify_otp(otp):
    """Check the OTP and open a Kronos session. A Result: true when we are logged in afterwards.

    session.json (the login the scraper uses) is only replaced once the new login works: a wrong code or a
    network error leaves the old login as it was."""
    try:
        response, session, token = login.validate_otp(otp, config.LOGIN_EMAIL)
        if not token:
            return Result(False, f"The OTP was not accepted (HTTP {response.status_code}). "
                                 "Check the code, or send a new one.")
        extra = {"erpToken": token}
        opened = login.establish_session(session, extra)
        if opened is None or opened.status_code != 200:
            code = opened.status_code if opened is not None else "-"
            return Result(False, f"The OTP was accepted but Kronos did not open a session (HTTP {code}). Try again.")
        answer = login.session_state(session, extra)
    except requests.RequestException as e:
        log.warning("otp verify failed: %s", e.__class__.__name__)
        return Result(False, "Kronos could not be reached - check the internet connection and try again.")
    if answer != OK:
        return Result(False, "Kronos could not be reached to confirm the login - try again." if answer == UNREACHABLE
                      else "Kronos did not accept the new login. Send a new OTP.")
    try:
        login.save_session(session, extra)
    except OSError as e:
        log.warning("could not save the login: %s", e)
        return Result(False, "Logged in, but session.json could not be written (is the folder read-only?).")
    login.clear_pending()
    _remember(OK)
    return Result(True, "Logged in.")
