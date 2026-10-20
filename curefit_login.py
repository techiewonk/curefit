"""
Curefit / Kronos scraper - login with manual OTP.

Flow:
  1. send_otp(email)        -> triggers OTP email
  2. validate_otp(email,otp)-> validates OTP, returns validate response (may contain erpToken / callback URL)
  3. establish_session()    -> hits kronos login with erpToken to set session cookies
  4. get_centers()          -> fetches list of centers

Usage:
  python curefit_login.py            # interactive: sends OTP, asks for it at the terminal
  python curefit_login.py login      # same as above

Manual / step-by-step:
  python curefit_login.py send
  python curefit_login.py validate 123456
  python curefit_login.py session
  python curefit_login.py centers

Session (cookies + erpToken) is persisted to session.json between runs.
"""
import sys
import json
import os
import requests

EMAIL = "shaurya@teamfit.in"
TENANT = "CUREFIT"
NAMESPACE = "kronos"
CALLBACK_URL = "https://kronos.curefit.co/login?next=/"

ACCOUNTS = "https://accounts.curefit.co"
KRONOS = "https://kronos.curefit.co"

SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

REFERER = (f"{ACCOUNTS}/login/?tenant={TENANT}&namespace={NAMESPACE}"
           "&callback=https%3A%2F%2Fkronos.curefit.co%2Flogin%3Fnext%3D%2F")


def new_session():
    s = requests.Session()
    s.headers.update({
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "origin": ACCOUNTS,
        "referer": REFERER,
        "user-agent": UA,
    })
    return s


def save_session(s, extra=None):
    data = {
        "cookies": requests.utils.dict_from_cookiejar(s.cookies),
        "extra": extra or {},
    }
    with open(SESSION_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_session():
    s = new_session()
    extra = {}
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE) as f:
            data = json.load(f)
        requests.utils.add_dict_to_cookiejar(s.cookies, data.get("cookies", {}))
        extra = data.get("extra", {})
    return s, extra


def send_otp(email=EMAIL):
    s = new_session()
    payload = {
        "email": email,
        "tenant": TENANT,
        "namespace": NAMESPACE,
        "callbackURL": CALLBACK_URL,
    }
    r = s.post(f"{ACCOUNTS}/login/otp/email/send", json=payload, timeout=30)
    print(f"[send] status={r.status_code}")
    print(f"[send] body={r.text}")
    save_session(s)
    return r


def validate_otp(otp, email=EMAIL):
    s, _ = load_session()
    payload = {
        "otp": str(otp),
        "email": email,
        "tenant": TENANT,
        "namespace": NAMESPACE,
        "callbackURL": CALLBACK_URL,
    }
    # don't auto-follow so we can inspect any redirect / erpToken
    r = s.post(f"{ACCOUNTS}/login/otp/email/validate", json=payload,
               allow_redirects=False, timeout=30)
    print(f"[validate] status={r.status_code}")
    print(f"[validate] headers={json.dumps(dict(r.headers), indent=2)}")
    print(f"[validate] cookies={requests.utils.dict_from_cookiejar(s.cookies)}")
    print(f"[validate] body={r.text}")

    erp_token = _extract_erp_token(r, s)
    save_session(s, {"erpToken": erp_token})
    print(f"[validate] erpToken={erp_token}")
    return r


def _extract_erp_token(r, s):
    # 1) from JSON body (raw, un-encoded token) - preferred
    try:
        body = r.json()
        for key in ("token", "erpToken"):
            if body.get(key):
                return body[key]
        for key in ("callbackURL", "redirectUrl", "url"):
            v = str(body.get(key, ""))
            if "erpToken=" in v:
                return v.split("erpToken=")[1].split("&")[0]
    except Exception:
        pass
    # 2) from Location header
    loc = r.headers.get("Location", "")
    if "erpToken=" in loc:
        return loc.split("erpToken=")[1].split("&")[0]
    # 3) from cookies (URL-encoded, last resort)
    from urllib.parse import unquote
    for name, val in requests.utils.dict_from_cookiejar(s.cookies).items():
        if name.lower() == "erptoken":
            return unquote(val)
    return None


def establish_session():
    s, extra = load_session()
    erp = extra.get("erpToken")
    if not erp:
        print("[session] no erpToken found; run validate first")
        return None
    from urllib.parse import quote
    url = f"{KRONOS}/login?next=%2F&erpToken={quote(erp, safe='')}"
    r = s.get(url, headers={
        "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8"),
        "referer": f"{ACCOUNTS}/",
    }, allow_redirects=True, timeout=30)
    print(f"[session] status={r.status_code} final_url={r.url}")
    print(f"[session] cookies={requests.utils.dict_from_cookiejar(s.cookies)}")
    save_session(s, extra)
    return r


def get_centers():
    s, extra = load_session()
    r = s.get(f"{KRONOS}/v1/user/centers", headers={
        "accept": "application/json",
        "referer": f"{KRONOS}/",
    }, timeout=30)
    print(f"[centers] status={r.status_code}")
    if r.status_code == 200:
        data = r.json()
        out = os.path.join(os.path.dirname(SESSION_FILE), "centers.json")
        with open(out, "w") as f:
            json.dump(data, f, indent=2)
        n = len(data) if isinstance(data, list) else "?"
        print(f"[centers] saved {n} centers -> {out}")
    else:
        print(f"[centers] body={r.text[:500]}")
    return r


def session_ok():
    """True if the saved session is accepted by Kronos (cheap authenticated call)."""
    s, extra = load_session()
    if not extra.get("erpToken"):
        return False
    try:
        r = s.get(f"{KRONOS}/v1/user/centers", headers={"accept": "application/json",
                                                         "referer": f"{KRONOS}/"}, timeout=30)
    except requests.RequestException:
        return False
    return r.status_code == 200


def interactive_login(email=EMAIL):
    """Full login: send OTP, ask for it at the terminal, validate + establish session."""
    from rich.prompt import Prompt
    from rich.panel import Panel
    from logutil import console, get_logger
    log = get_logger()

    console.print(Panel.fit(f"[bold]Curefit login[/bold]\naccount: [cyan]{email}[/cyan]",
                            border_style="cyan"))
    with console.status("[cyan]Sending OTP...[/cyan]"):
        r = send_otp(email)
    if r.status_code != 200:
        log.error("otp_send_failed", status=r.status_code, body=r.text[:200])
        return
    log.info("otp_sent", email=email)
    console.print(f"[green]OTP sent[/green] to [cyan]{email}[/cyan] — check the inbox.")

    for attempt in range(1, 4):
        otp = Prompt.ask("[bold]Enter the OTP[/bold]").strip()
        with console.status("[cyan]Validating OTP...[/cyan]"):
            vr = validate_otp(otp, email)
        try:
            ok = "token" in vr.json()
        except Exception:
            ok = False
        if vr.status_code == 200 and ok:
            log.info("otp_validated")
            break
        log.warning("otp_invalid", attempt=attempt, status=vr.status_code)
        console.print(f"[red]OTP not accepted[/red] (attempt {attempt}/3). Try again.")
    else:
        console.print("[red]Login failed after 3 attempts.[/red]")
        return

    with console.status("[cyan]Establishing Kronos session...[/cyan]"):
        sr = establish_session()
    if sr is not None and sr.status_code == 200:
        log.info("session_established", final_url=sr.url)
        console.print(Panel.fit("[bold green]Logged in.[/bold green] "
                                "You can now run [bold]python scrape_sales.py[/bold]",
                                border_style="green"))
    else:
        console.print("[red]Could not establish session.[/red]")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "login"
    if cmd == "send":
        send_otp()
    elif cmd == "validate":
        validate_otp(sys.argv[2])
    elif cmd == "session":
        establish_session()
    elif cmd == "centers":
        get_centers()
    elif cmd == "login":  # interactive: send -> prompt OTP -> validate -> session
        interactive_login()
    else:
        print(__doc__)
