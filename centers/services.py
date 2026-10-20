"""Center list logic shared by the Kronos sync, the legacy import and the admin."""
import datetime as dt
import json
import logging
import re
from pathlib import Path

from django.db import transaction

from .models import Center, CenterList, CenterSyncLog

logger = logging.getLogger(__name__)

# light extras kept in Center.raw (the full Kronos record has photos/documents we do not need)
RAW_KEYS = ("shortName", "timezone", "businessStartTime", "businessEndTime", "placeUrl",
            "tenantID", "version")


def _date(value):
    try:
        return dt.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def center_fields(c):
    """Kronos record -> Center field values."""
    return {
        "name": (c.get("name") or "").strip(),
        "code": c.get("centerCode") or "",
        "state": c.get("state") or "",
        "center_type": c.get("centerType") or "",
        "locality": c.get("locality") or "",
        "cluster": c.get("managementClusterArea") or "",
        "city_id": c.get("cityID"),
        "country_id": c.get("countryID"),
        "capacity": c.get("capacity"),
        "upcoming": bool(c.get("upcoming")),
        "sale_disabled": bool(c.get("saleDisabled")),
        "is_published": bool(c.get("isPublished")),
        "phone": c.get("phone") or "",
        "launch_date": _date(c.get("launchDate")),
        "shutdown_date": _date(c.get("shutdownDate")),
        "center_service_id": c.get("centerServiceId"),
        "seller_id": c.get("sellerId") or "",
        "raw": {k: c[k] for k in RAW_KEYS if k in c},
    }


TEXT_FIELDS = ("name", "code", "state", "center_type", "locality", "cluster", "phone", "seller_id")
TINY_SHARE, TINY_FROM = 0.5, 20  # a list under half the known centers (when 20+ are known) is a bad answer


def _usable(raw_centers):
    """{id: record}: records without a usable id are skipped (and logged), a repeated id keeps its first record."""
    out, skipped = {}, 0
    for raw in raw_centers or []:
        try:
            cid = int(raw["id"])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        out.setdefault(cid, raw)
    if skipped:
        logger.warning("center sync: skipped %d record(s) without a usable id", skipped)
    return out


@transaction.atomic
def upsert_from_kronos(raw_centers, log=True):
    """Create/update centers from Kronos records and report what changed.

    Records without a usable id are skipped and a repeated id counts once. A blank name, state or other text from
    Kronos never replaces a known value. Centers missing from the list are kept (past data still refers to them)
    but marked REMOVED, so they stop counting as ACTIVE. An empty list, or one with under half the known centers,
    is refused (ValueError) and changes nothing.
    """
    existing = {c.center_id: c for c in Center.objects.all()}
    records = _usable(raw_centers)
    known = sum(1 for c in existing.values() if c.state != Center.REMOVED)
    if not records or (known >= TINY_FROM and len(records) < TINY_SHARE * known):
        raise ValueError(f"Kronos returned {len(records)} usable center(s) (we know {known}): "
                         "that does not look like the full list - nothing was changed")
    seen, added, state_changed, renamed = set(), [], [], []
    to_create, to_update = [], []
    for cid, raw in records.items():
        seen.add(cid)
        fields = center_fields(raw)
        old = existing.get(cid)
        if old is not None:
            for k in TEXT_FIELDS:  # a field Kronos left out must not wipe what we know
                if not fields[k] and getattr(old, k):
                    fields[k] = getattr(old, k)
            if fields["state"] == Center.REMOVED:  # it is back in the list: REMOVED no longer holds, even unknown
                fields["state"] = raw.get("state") or ""
        if old is None:
            to_create.append(Center(center_id=cid, **fields))
            added.append({"id": cid, "name": fields["name"], "state": fields["state"]})
            continue
        if old.state != fields["state"]:
            state_changed.append({"id": cid, "name": fields["name"], "from": old.state,
                                  "to": fields["state"]})
        if old.name != fields["name"]:
            renamed.append({"id": cid, "from": old.name, "to": fields["name"]})
        if any(getattr(old, k) != v for k, v in fields.items()):
            for k, v in fields.items():
                setattr(old, k, v)
            to_update.append(old)
    Center.objects.bulk_create(to_create, batch_size=500)
    if to_update:
        Center.objects.bulk_update(to_update, list(center_fields({}).keys()), batch_size=500)
    gone = [c for cid, c in existing.items() if cid not in seen and c.state != Center.REMOVED]
    removed = [{"id": c.center_id, "name": c.name, "from": c.state} for c in gone]
    Center.objects.filter(pk__in=[c.pk for c in gone]).update(state=Center.REMOVED)
    diff = {"added": added, "removed": removed, "state_changed": state_changed,
            "renamed": renamed, "total": len(seen),
            "active": sum(1 for r in records.values() if r.get("state") == "ACTIVE")}
    if log:
        CenterSyncLog.objects.create(**diff)
    return diff


def get_list(name):
    return CenterList.objects.get_or_create(name=name)[0]


def list_ids(name):
    """Center ids of a list, ascending."""
    return list(get_list(name).centers.order_by("center_id").values_list("center_id", flat=True))


def set_list(name, ids):
    """Replace a list's centers; unknown ids are rejected."""
    ids = {int(i) for i in ids}
    known = set(Center.objects.filter(center_id__in=ids).values_list("center_id", flat=True))
    if ids - known:
        raise ValueError(f"Unknown center ids: {sorted(ids - known)[:10]} — update the center list first")
    lst = get_list(name)
    lst.centers.set(ids)
    inactive = list(Center.objects.filter(center_id__in=ids).exclude(state="ACTIVE")
                    .values_list("center_id", flat=True))
    return {"count": len(ids), "inactive": inactive}


GROUP_SEED = Path(__file__).with_name("group_seed.json")


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def apply_group_seed(path=None):
    """Put the gyms of the management sheet into their groups, matching by name.

    Only groups that exist and are still empty are filled, so a gym you took out (or a group you deleted
    or emptied on purpose) is not put back on the next start. A name that matches no center or more than one
    is reported and left alone. Returns {"added": n, "groups": {name: n}, "unmatched": [...]}.
    """
    from .models import Center, CenterGroup
    seed = json.loads(Path(path or GROUP_SEED).read_text(encoding="utf-8"))
    by_name = {}
    for c in Center.objects.all():
        by_name.setdefault(_norm(c.name), []).append(c.pk)
    done, unmatched = {}, []
    for group in CenterGroup.objects.filter(name__in=list(seed)):
        if group.centers.exists():
            continue
        ids = []
        for name in seed[group.name]:
            found = by_name.get(_norm(name), [])
            if len(found) == 1:
                ids.append(found[0])
            else:
                unmatched.append(f"{group.name}: {name} ({'no center' if not found else f'{len(found)} centers'})")
        group.centers.add(*ids)
        done[group.name] = len(ids)
    return {"added": sum(done.values()), "groups": done, "unmatched": unmatched}
