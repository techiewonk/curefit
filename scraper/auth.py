"""Kronos login (the emailed OTP) as a small service over `scraper.login`."""
import time

from constance import config

from . import login

CACHE_SECONDS = 30
_cache = {"at": 0.0, "ok": False}


def logged_in(fresh=False):
    """True if Kronos accepts the saved session. The answer is cached briefly (it costs a request)."""
    if fresh or time.time() - _cache["at"] > CACHE_SECONDS:
        _cache.update(at=time.time(), ok=login.session_ok())
    return _cache["ok"]


def send_otp():
    """Ask Kronos to email an OTP to the configured account. Returns True if it was sent."""
    return login.send_otp(config.LOGIN_EMAIL).status_code == 200


def verify_otp(otp):
    """Check the OTP and open a Kronos session. Returns True when we are logged in afterwards."""
    response = login.validate_otp(otp, config.LOGIN_EMAIL)
    try:
        accepted = response.status_code == 200 and "token" in response.json()
    except ValueError:
        accepted = False
    if accepted:
        session = login.establish_session()
        accepted = session is not None and session.status_code == 200
    _cache.update(at=time.time(), ok=bool(accepted) and login.session_ok())
    return _cache["ok"]
