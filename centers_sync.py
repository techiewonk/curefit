"""
Keep the center list current.

  python centers_sync.py                       refresh centers.json + centers.csv from Kronos
  python centers_sync.py --list actual --state ACTIVE
                                               refresh, then rebuild centers_to_process.csv
                                               from the filters
  python centers_sync.py --list test --ids 1,7,15
                                               set the test list explicitly

Needs a valid login (run.bat / the admin's Run tab). The admin's Centers tab uses the same
functions. Two lists exist: "actual" (centers_to_process.csv, the real run) and "test"
(centers_test.csv, a handful of centers for trial runs).
"""
import argparse
import csv
import json
import os
import tempfile
import time

import requests

import curefit_login as C

HERE = os.path.dirname(os.path.abspath(__file__))
CENTERS_JSON = os.path.join(HERE, "centers.json")
CENTERS_CSV = os.path.join(HERE, "centers.csv")
LISTS = {"actual": os.path.join(HERE, "centers_to_process.csv"),
         "test": os.path.join(HERE, "centers_test.csv")}

FLAT_FIELDS = ["id", "name", "centerCode", "state", "centerType", "locality", "cityID",
               "countryID", "managementClusterArea", "capacity", "upcoming", "saleDisabled",
               "isPublished", "phone", "launchDate", "shutdownDate", "centerServiceId", "sellerId"]


def _atomic_write(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def load_raw():
    if not os.path.exists(CENTERS_JSON):
        return {"centers": []}
    with open(CENTERS_JSON, encoding="utf-8") as f:
        return json.load(f)


def load_centers():
    """Flat rows for every known center (what the admin shows)."""
    out = []
    for c in load_raw().get("centers", []):
        row = {k: c.get(k) for k in FLAT_FIELDS}
        row["id"] = str(c["id"])
        row["name"] = (c.get("name") or "").strip()
        out.append(row)
    return sorted(out, key=lambda r: int(r["id"]) if r["id"].isdigit() else 0)


def synced_at():
    return (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(os.path.getmtime(CENTERS_JSON)))
            if os.path.exists(CENTERS_JSON) else None)


def _diff(old, new):
    o = {str(c["id"]): c for c in old}
    n = {str(c["id"]): c for c in new}
    nm = lambda c: (c.get("name") or "").strip()
    return {
        "added": [{"id": i, "name": nm(n[i]), "state": n[i].get("state")} for i in n if i not in o],
        "removed": [{"id": i, "name": nm(o[i])} for i in o if i not in n],
        "state_changed": [{"id": i, "name": nm(n[i]), "from": o[i].get("state"),
                           "to": n[i].get("state")}
                          for i in n if i in o and o[i].get("state") != n[i].get("state")],
        "renamed": [{"id": i, "from": nm(o[i]), "to": nm(n[i])}
                    for i in n if i in o and nm(o[i]) != nm(n[i])],
    }


def sync_centers():
    """Fetch the center list from Kronos and replace centers.json / centers.csv.

    Nothing is written unless the response looks like a real center list, so a bad response
    can never wipe the file. Returns the changes since the previous sync.
    """
    s, extra = C.load_session()
    if not extra.get("erpToken"):
        raise PermissionError("Not logged in")
    r = s.get(f"{C.KRONOS}/v1/user/centers",
              headers={"accept": "application/json", "referer": f"{C.KRONOS}/"}, timeout=60)
    if r.status_code in (401, 403):
        raise PermissionError(f"Kronos rejected the session (HTTP {r.status_code})")
    r.raise_for_status()
    data = r.json()
    centers = data.get("centers") if isinstance(data, dict) else None
    if not isinstance(centers, list) or not centers or not all("id" in c for c in centers):
        raise ValueError("Unexpected response from Kronos — centers.json was not changed")

    old = load_raw().get("centers", [])
    _atomic_write(CENTERS_JSON, json.dumps(data, indent=2))
    rows = load_centers()
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FLAT_FIELDS, lineterminator="\n")
    w.writeheader()
    for row in rows:
        w.writerow({k: ("" if v is None else v) for k, v in row.items()})
    _atomic_write(CENTERS_CSV, buf.getvalue())

    d = _diff(old, centers)
    d.update(total=len(centers), active=sum(1 for c in centers if c.get("state") == "ACTIVE"),
             synced_at=synced_at())
    return d


# ------------------------------ lists ------------------------------
def read_list(name):
    path = LISTS[name]
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if rows and "center_id" in rows[0]:
        return [r["center_id"].strip() for r in rows
                if (r.get("center_id") or "").strip() and not r["center_id"].startswith("#")]
    with open(path, encoding="utf-8-sig") as f:  # plain list of ids
        return [ln.strip() for ln in f if ln.strip() and ln.strip().lower() != "center_id"
                and not ln.startswith("#")]


def write_list(name, ids):
    """Write center_id,center_name; unknown ids are rejected, duplicates dropped."""
    known = {c["id"]: c for c in load_centers()}
    clean, seen = [], set()
    for i in ids:
        i = str(i).strip()
        if not i.isdigit():
            raise ValueError(f"Not a center id: {i!r}")
        if i not in known:
            raise ValueError(f"Center {i} is not in centers.json — sync the center list first")
        if i not in seen:
            seen.add(i)
            clean.append(i)
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["center_id", "center_name"])
    for i in sorted(clean, key=int):
        w.writerow([i, known[i]["name"]])
    _atomic_write(LISTS[name], buf.getvalue())
    inactive = [i for i in clean if known[i].get("state") != "ACTIVE"]
    return {"count": len(clean), "inactive": inactive}


def filter_ids(state=None, cluster=None, center_type=None):
    out = []
    for c in load_centers():
        if state and c["state"] != state:
            continue
        if cluster and c["managementClusterArea"] != cluster:
            continue
        if center_type and c["centerType"] != center_type:
            continue
        out.append(c["id"])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--no-sync", action="store_true", help="do not contact Kronos")
    ap.add_argument("--list", choices=list(LISTS), help="rebuild this list")
    ap.add_argument("--ids", help="comma-separated center ids for --list")
    ap.add_argument("--state", help="filter for --list, e.g. ACTIVE")
    ap.add_argument("--cluster", help="filter for --list, managementClusterArea")
    ap.add_argument("--type", dest="ctype", help="filter for --list, e.g. BLACK")
    args = ap.parse_args()

    if not args.no_sync:
        try:
            d = sync_centers()
        except (PermissionError, requests.RequestException, ValueError) as e:
            raise SystemExit(f"Sync failed: {e}")
        print(f"Synced {d['total']} centers ({d['active']} active) at {d['synced_at']}")
        for k in ("added", "removed", "state_changed", "renamed"):
            if d[k]:
                print(f"  {k}: {len(d[k])}  e.g. {d[k][:3]}")
    if args.list:
        ids = [x for x in (args.ids or "").split(",") if x.strip()] if args.ids else \
            filter_ids(args.state, args.cluster, args.ctype)
        res = write_list(args.list, ids)
        print(f"Wrote {res['count']} centers to {LISTS[args.list]}"
              + (f" ({len(res['inactive'])} not ACTIVE)" if res["inactive"] else ""))


if __name__ == "__main__":
    main()
