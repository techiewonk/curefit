"""Kronos login: an OTP is emailed, we exchange it for a session that is saved in session.json.

A login in progress (OTP sent, not verified yet) lives in session.pending.json, so sending an OTP or typing a
wrong one never throws away a login that still works: session.json is only replaced once the new login has been
checked with Kronos.

Nothing here prints or logs tokens, cookies or the OTP.
"""
import json
import logging
import os
import tempfile
import time
from urllib.parse import quote, unquote

import requests
from django.conf import settings

log = logging.getLogger("scraper.login")

TENANT, NAMESPACE = "CUREFIT", "kronos"
ACCOUNTS = "https://accounts.curefit.co"
KRONOS = "https://kronos.curefit.co"
CALLBACK_URL = f"{KRONOS}/login?next=/"
REFERER = (f"{ACCOUNTS}/login/?tenant={TENANT}&namespace={NAMESPACE}"
           "&callback=https%3A%2F%2Fkronos.curefit.co%2Flogin%3Fnext%3D%2F")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
TIMEOUT = 30
PROBE_TIMEOUT = 5  # the "are we logged in?" check: short, it runs while a page waits
SESSION_FILE = settings.BASE_DIR / "session.json"  # git-ignored: it holds the login
PENDING_FILE = settings.BASE_DIR / "session.pending.json"  # a login in progress (OTP sent, not verified yet)
OK, REJECTED, UNREACHABLE = "ok", "rejected", "unreachable"


def new_session():
    s = requests.Session()
    s.headers.update({"accept": "application/json, text/plain, */*", "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
                      "content-type": "application/json", "origin": ACCOUNTS, "referer": REFERER, "user-agent": UA})
    return s


def _write_json(path, data):
    """Write to a temporary file next to `path`, then swap it in: a reader never sees a half-written file.

    Windows refuses the swap while another process has the file open for a moment: it is tried a few times.
    The temporary file is always removed."""
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        for attempt in range(5):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
    finally:
        if os.path.exists(temp):
            os.remove(temp)


def save_session(s, extra=None, path=None):
    _write_json(path or SESSION_FILE, {"cookies": requests.utils.dict_from_cookiejar(s.cookies), "extra": extra or {}})


def load_session(path=None):
    """(requests session carrying the saved cookies, extra data such as the erpToken).

    A missing, unreadable or damaged file counts as "not logged in" instead of an error."""
    path = path or SESSION_FILE
    s, extra = new_session(), {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return s, extra
    except (OSError, ValueError) as e:
        log.warning("could not read %s (%s): treated as not logged in", path.name, e.__class__.__name__)
        return s, extra
    if isinstance(data, dict):
        cookies = data.get("cookies")
        if isinstance(cookies, dict):
            requests.utils.add_dict_to_cookiejar(s.cookies, cookies)
        extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
    return s, extra


def clear_pending():
    try:
        PENDING_FILE.unlink()
    except OSError:
        pass


def _payload(email, **more):
    return {"email": email, "tenant": TENANT, "namespace": NAMESPACE, "callbackURL": CALLBACK_URL, **more}


def send_otp(email):
    """Ask for an OTP. The new login is kept aside (pending); session.json is not touched. Network errors raise."""
    s = new_session()
    r = s.post(f"{ACCOUNTS}/login/otp/email/send", json=_payload(email), timeout=TIMEOUT)
    if r.status_code == 200:
        save_session(s, path=PENDING_FILE)
    log.info("otp send: HTTP %s", r.status_code)
    return r


def validate_otp(otp, email):
    """Check the OTP on the pending login. Returns (response, requests session, token or None). Network errors raise."""
    s, _ = load_session(PENDING_FILE)
    r = s.post(f"{ACCOUNTS}/login/otp/email/validate", json=_payload(email, otp=str(otp)),
               allow_redirects=False, timeout=TIMEOUT)
    token = _erp_token(r, s)
    log.info("otp validate: HTTP %s, token captured: %s", r.status_code, bool(token))
    return r, s, token


def _erp_token(r, s):
    """The ERP token from the validate response: JSON body, then Location header, then cookie."""
    try:
        body = r.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        for key in ("token", "erpToken"):
            if body.get(key):
                return str(body[key])
        for key in ("callbackURL", "redirectUrl", "url"):
            if "erpToken=" in str(body.get(key, "")):
                return str(body[key]).split("erpToken=")[1].split("&")[0]
    location = r.headers.get("Location", "")
    if "erpToken=" in location:
        return location.split("erpToken=")[1].split("&")[0]
    for name, value in requests.utils.dict_from_cookiejar(s.cookies).items():
        if name.lower() == "erptoken":
            return unquote(value)
    return None


def establish_session(s=None, extra=None):
    """Open the Kronos session with the ERP token (sets the cookies the scraper needs) on `s` (default: the saved
    login, which is then saved again). Returns the response, or None without a token."""
    saved = s is None
    if saved:
        s, extra = load_session()
    if not (extra or {}).get("erpToken"):
        return None
    r = s.get(f"{KRONOS}/login?next=%2F&erpToken={quote(extra['erpToken'], safe='')}",
              headers={"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                       "referer": f"{ACCOUNTS}/"}, allow_redirects=True, timeout=TIMEOUT)
    if saved:
        save_session(s, extra)
    log.info("kronos session: HTTP %s", r.status_code)
    return r


def session_state(s=None, extra=None):
    """OK when Kronos accepts the login (one cheap call, at most PROBE_TIMEOUT seconds), REJECTED when there is
    no login or Kronos refuses it, UNREACHABLE when Kronos could not be asked (offline, timeout, a 5xx)."""
    if s is None:
        s, extra = load_session()
    if not (extra or {}).get("erpToken"):
        return REJECTED
    try:
        r = s.get(f"{KRONOS}/v1/user/centers", headers={"accept": "application/json", "referer": f"{KRONOS}/"},
                  timeout=PROBE_TIMEOUT)
    except requests.RequestException:
        return UNREACHABLE
    if r.status_code == 200:
        return OK
    return UNREACHABLE if r.status_code >= 500 else REJECTED


def session_ok():
    """True if Kronos accepts the saved session."""
    return session_state() == OK
