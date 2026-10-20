"""Kronos login: an OTP is emailed, we exchange it for a session that is saved in session.json.

Nothing here prints or logs tokens, cookies or the OTP.
"""
import json
import logging
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
SESSION_FILE = settings.BASE_DIR / "session.json"  # git-ignored: it holds the login


def new_session():
    s = requests.Session()
    s.headers.update({"accept": "application/json, text/plain, */*", "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
                      "content-type": "application/json", "origin": ACCOUNTS, "referer": REFERER, "user-agent": UA})
    return s


def save_session(s, extra=None):
    """Write to a temporary file, then swap it in: a reader never sees a half-written session."""
    temp = SESSION_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps({"cookies": requests.utils.dict_from_cookiejar(s.cookies),
                                "extra": extra or {}}, indent=2), encoding="utf-8")
    temp.replace(SESSION_FILE)


def load_session():
    """(requests session carrying the saved cookies, extra data such as the erpToken)."""
    s, extra = new_session(), {}
    if SESSION_FILE.exists():
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        requests.utils.add_dict_to_cookiejar(s.cookies, data.get("cookies", {}))
        extra = data.get("extra", {})
    return s, extra


def _payload(email, **more):
    return {"email": email, "tenant": TENANT, "namespace": NAMESPACE, "callbackURL": CALLBACK_URL, **more}


def send_otp(email):
    s = new_session()
    r = s.post(f"{ACCOUNTS}/login/otp/email/send", json=_payload(email), timeout=TIMEOUT)
    save_session(s)
    log.info("otp send: HTTP %s", r.status_code)
    return r


def validate_otp(otp, email):
    s, _ = load_session()
    r = s.post(f"{ACCOUNTS}/login/otp/email/validate", json=_payload(email, otp=str(otp)),
               allow_redirects=False, timeout=TIMEOUT)
    token = _erp_token(r, s)
    save_session(s, {"erpToken": token})
    log.info("otp validate: HTTP %s, token captured: %s", r.status_code, bool(token))
    return r


def _erp_token(r, s):
    """The ERP token from the validate response: JSON body, then Location header, then cookie."""
    try:
        body = r.json()
        for key in ("token", "erpToken"):
            if body.get(key):
                return body[key]
        for key in ("callbackURL", "redirectUrl", "url"):
            if "erpToken=" in str(body.get(key, "")):
                return str(body[key]).split("erpToken=")[1].split("&")[0]
    except ValueError:
        pass
    location = r.headers.get("Location", "")
    if "erpToken=" in location:
        return location.split("erpToken=")[1].split("&")[0]
    for name, value in requests.utils.dict_from_cookiejar(s.cookies).items():
        if name.lower() == "erptoken":
            return unquote(value)
    return None


def establish_session():
    """Open the Kronos session with the ERP token (sets the cookies the scraper needs)."""
    s, extra = load_session()
    if not extra.get("erpToken"):
        return None
    r = s.get(f"{KRONOS}/login?next=%2F&erpToken={quote(extra['erpToken'], safe='')}",
              headers={"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                       "referer": f"{ACCOUNTS}/"}, allow_redirects=True, timeout=TIMEOUT)
    save_session(s, extra)
    log.info("kronos session: HTTP %s", r.status_code)
    return r


def session_ok():
    """True if Kronos accepts the saved session (one cheap authenticated call)."""
    s, extra = load_session()
    if not extra.get("erpToken"):
        return False
    try:
        r = s.get(f"{KRONOS}/v1/user/centers", headers={"accept": "application/json", "referer": f"{KRONOS}/"},
                  timeout=TIMEOUT)
    except requests.RequestException:
        return False
    return r.status_code == 200
