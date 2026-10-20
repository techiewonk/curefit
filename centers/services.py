"""Center list logic shared by the Kronos sync, the legacy import and the admin."""
import datetime as dt

from django.db import transaction

from .models import Center, CenterList, CenterSyncLog

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


@transaction.atomic
def upsert_from_kronos(raw_centers, log=True):
    """Create/update centers from Kronos records and report what changed.

    Centers that disappeared from Kronos are reported but kept (past data still refers to them).
    """
    existing = {c.center_id: c for c in Center.objects.all()}
    seen, added, state_changed, renamed = set(), [], [], []
    to_create, to_update = [], []
    for raw in raw_centers:
        cid = int(raw["id"])
        seen.add(cid)
        fields = center_fields(raw)
        old = existing.get(cid)
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
    removed = [{"id": cid, "name": c.name} for cid, c in existing.items() if cid not in seen]
    diff = {"added": added, "removed": removed, "state_changed": state_changed,
            "renamed": renamed, "total": len(seen),
            "active": sum(1 for r in raw_centers if r.get("state") == "ACTIVE")}
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
