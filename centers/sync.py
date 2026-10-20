"""Refresh the center list from Kronos (needs a valid login)."""
from scraper import login

from . import services

URL = f"{login.KRONOS}/v1/user/centers"


class NotLoggedIn(Exception):
    pass


def sync_from_kronos(session=None):
    """Fetch every center the account can see and upsert it. Returns the change summary.

    Nothing is written unless the response looks like a real center list, so a bad response can
    never wipe the table.
    """
    http, extra = (session, {"erpToken": True}) if session is not None else login.load_session()
    if not extra.get("erpToken"):
        raise NotLoggedIn("Not logged in")
    r = http.get(URL, headers={"accept": "application/json", "referer": f"{login.KRONOS}/"}, timeout=60)
    if r.status_code in (401, 403):
        raise NotLoggedIn(f"Kronos rejected the session (HTTP {r.status_code})")
    r.raise_for_status()
    data = r.json()
    centers = data.get("centers") if isinstance(data, dict) else None
    if not isinstance(centers, list) or not centers or not all("id" in c for c in centers):
        raise ValueError("Unexpected response from Kronos - the center list was not changed")
    summary = services.upsert_from_kronos(centers)
    services.apply_group_seed()  # the first sync gives the groups their gyms
    return summary
