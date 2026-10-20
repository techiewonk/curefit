"""Customers: tie every sales / gym row to one person and keep that person's newest status.

Identity: the Kronos user id comes first. A person can have several phone numbers (each kept once,
as the last 10 digits). A row with a phone but no user id joins whoever owns that phone. A phone
seen with two different user ids is flagged `shared` and the two people are NOT merged.

Status: the newest session wins (compared by its end date); an older session run later never
overwrites a newer status. The rows themselves stay on their sessions.
"""
import datetime as dt
import re

from constance import config as settings
from django.db.models import Case, Value, When
from django.utils import timezone

from .models import Customer, CustomerPhone, GymMember, SalesRecord, Session

BATCH = 1000
_JUNK = {"", "none", "nan", "null", "-"}


def clean_user_id(value):
    value = str(value).strip() if value is not None else ""
    return None if value.lower() in _JUNK else value[:40]


def normalise_phone(value):
    """The last 10 digits of a phone number (drops +91, 0, spaces, dashes); None when it is not one."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else None


def _in(queryset, field, values, *fields):
    """Rows whose `field` is among `values`, asked in chunks (SQLite allows few parameters per query)."""
    values = list(values)
    for i in range(0, len(values), 900):
        chunk = queryset.filter(**{f"{field}__in": values[i:i + 900]})
        yield from (chunk.values_list(*fields) if fields else chunk)


def _rows(session):
    """Sales and gym rows of the session as one list of plain dicts."""
    out = []
    for r in SalesRecord.objects.filter(session=session).values(
            "id", "user_id", "name", "phone", "pack_name", "member_end_date", "renewed", "active_last_30d",
            "revenue", "center_id", "scraped_at"):
        out.append({"kind": "sales", "id": r["id"], "uid": clean_user_id(r["user_id"]), "phone": normalise_phone(r["phone"]),
                    "name": r["name"], "pack": r["pack_name"], "end": r["member_end_date"], "renewed": r["renewed"],
                    "active": r["active_last_30d"], "amount": r["revenue"], "center": r["center_id"],
                    "ts": r["scraped_at"]})
    for r in GymMember.objects.filter(session=session).values(
            "id", "user_id", "username", "pack_name", "pack_end_date", "final_amount_paid", "status",
            "new_vs_repeat", "last_class_date", "center_id", "scraped_at"):
        out.append({"kind": "gym", "id": r["id"], "uid": clean_user_id(r["user_id"]), "phone": None,
                    "name": r["username"], "pack": r["pack_name"], "end": r["pack_end_date"],
                    "status": r["status"], "nvr": r["new_vs_repeat"], "last_class": r["last_class_date"],
                    "amount": r["final_amount_paid"], "center": r["center_id"], "ts": r["scraped_at"]})
    return out


def _resolve(rows):
    """Give every row a customer id, creating customers and registering phones as needed."""
    uids = {r["uid"] for r in rows if r["uid"]}
    phones = {r["phone"] for r in rows if r["phone"]}
    by_uid = dict(_in(Customer.objects, "user_id", uids, "user_id", "id"))
    owner = {p.phone: p for p in _in(CustomerPhone.objects, "phone", phones)}

    # a person known only by phone who now shows up with a user id: that is the same person
    claimed = []
    phone_only = {c.pk: c for c in _in(Customer.objects.filter(user_id__isnull=True), "phones__phone", phones)}
    for r in rows:
        if r["uid"] and r["uid"] not in by_uid and r["phone"] in owner:
            c = phone_only.pop(owner[r["phone"]].customer_id, None)
            if c:
                c.user_id = r["uid"]
                claimed.append(c)
                by_uid[r["uid"]] = c.pk
    if claimed:
        Customer.objects.bulk_update(claimed, ["user_id"], batch_size=BATCH)

    new = [Customer(user_id=u) for u in sorted(uids - set(by_uid))]
    Customer.objects.bulk_create(new, batch_size=BATCH)
    by_uid.update(_in(Customer.objects, "user_id", [c.user_id for c in new], "user_id", "id"))

    # people only known by phone
    with_uid = {r["phone"] for r in rows if r["uid"] and r["phone"]}
    new_phone_only = sorted({r["phone"] for r in rows
                             if not r["uid"] and r["phone"] and r["phone"] not in owner and r["phone"] not in with_uid})
    stubs = Customer.objects.bulk_create([Customer() for _ in new_phone_only], batch_size=BATCH)
    ids = [c.pk for c in stubs]
    if len(ids) != len(new_phone_only) or not all(ids):  # backends that do not return the new ids
        ids = list(Customer.objects.filter(user_id__isnull=True).order_by("-id").values_list("id", flat=True)[:len(new_phone_only)])[::-1]
    fresh = [CustomerPhone(phone=p, customer_id=cid) for p, cid in zip(new_phone_only, ids)]

    # register each (user, phone) pair once; a phone owned by someone else becomes 'shared'
    shared = set()
    for r in rows:
        if r["uid"] and r["phone"]:
            cid = by_uid[r["uid"]]
            if r["phone"] in owner:
                if owner[r["phone"]].customer_id != cid:
                    shared.add(r["phone"])
            else:
                owner[r["phone"]] = CustomerPhone(phone=r["phone"], customer_id=cid)
                fresh.append(owner[r["phone"]])
    CustomerPhone.objects.bulk_create(fresh, batch_size=BATCH, ignore_conflicts=True)
    for i in range(0, len(shared), 900):
        CustomerPhone.objects.filter(phone__in=list(shared)[i:i + 900]).update(shared=True)
    owner_id = dict(_in(CustomerPhone.objects, "phone", phones, "phone", "customer_id"))

    for r in rows:
        r["customer"] = by_uid.get(r["uid"]) if r["uid"] else owner_id.get(r["phone"])


def _link_rows(rows):
    for model, kind in ((SalesRecord, "sales"), (GymMember, "gym")):
        objs = [model(pk=r["id"], customer_id=r["customer"]) for r in rows if r["kind"] == kind and r["customer"]]
        model.objects.bulk_update(objs, ["customer"], batch_size=BATCH)


def _newest(rows, key):
    return max(rows, key=key) if rows else None


def _snapshot(session, rows):
    """Overwrite each customer's status with what this session says, unless a newer session already did."""
    by_customer = {}
    for r in rows:
        if r["customer"]:
            by_customer.setdefault(r["customer"], []).append(r)
    customers = {c.pk: c for c in _in(Customer.objects, "pk", by_customer)}
    now, changed = timezone.now(), []
    for cid, mine in by_customer.items():
        c = customers[cid]
        if c.as_of and c.as_of > session.end_date:
            continue  # this session is older than what we already know
        pack = _newest(mine, lambda r: (r["end"] or dt.date.min, r["ts"]))
        sales = _newest([r for r in mine if r["kind"] == "sales"], lambda r: (r["end"] or dt.date.min, r["ts"]))
        gym = _newest([r for r in mine if r["kind"] == "gym"], lambda r: (r["end"] or dt.date.min, r["ts"]))
        c.name = next((r["name"] for r in (pack, sales, gym) if r and r["name"]), c.name)
        if c.pack_end_date is None or (pack["end"] and pack["end"] >= c.pack_end_date):
            # the pack only moves forward: a short session that shows an older, expired pack must not replace it
            c.center_id, c.pack_name = pack["center"], pack["pack"] or c.pack_name
            c.pack_end_date = pack["end"] or c.pack_end_date
        if sales:
            c.renewed, c.active_last_30d = sales["renewed"] or c.renewed, sales["active"] or c.active_last_30d
            c.last_amount = sales["amount"] if sales["amount"] is not None else c.last_amount
        if gym:
            c.member_status, c.new_vs_repeat = gym["status"] or c.member_status, gym["nvr"] or c.new_vs_repeat
            c.last_class_date = max(filter(None, [c.last_class_date, gym["last_class"]]), default=None)
            c.last_amount = gym["amount"] if gym["amount"] is not None else c.last_amount
        c.as_of, c.last_session, c.updated_at = session.end_date, session, now
        changed.append(c)
    Customer.objects.bulk_update(changed, [
        "name", "center", "pack_name", "pack_end_date", "renewed", "active_last_30d", "member_status",
        "new_vs_repeat", "last_class_date", "last_amount", "as_of", "last_session", "updated_at"], batch_size=BATCH)
    return len(changed)


def refresh_leads(today=None):
    """Recompute the lead status of every customer from the pack end date (thresholds are in Settings)."""
    today = today or timezone.localdate()
    soon = today + dt.timedelta(days=settings.LEAD_EXPIRING_DAYS)
    recent = today - dt.timedelta(days=settings.LEAD_EXPIRED_DAYS)
    return Customer.objects.update(lead_status=Case(
        When(pack_end_date__isnull=True, then=Value(Customer.UNKNOWN)),
        When(pack_end_date__gt=soon, then=Value(Customer.ACTIVE)),
        When(pack_end_date__gte=today, then=Value(Customer.EXPIRING)),
        When(pack_end_date__gte=recent, then=Value(Customer.EXPIRED)),
        default=Value(Customer.LAPSED)))


def link_session(session):
    """Link one session's rows to customers and update their status. Safe to repeat."""
    session = Session.objects.get(pk=session.pk)  # dates as dates, even if the caller built it from text
    rows = _rows(session)
    _resolve(rows)
    _link_rows(rows)
    n = _snapshot(session, rows)
    refresh_leads()
    return n


def link_pending():
    """Sessions that still have unlinked rows, oldest first (so the newest status ends up on top)."""
    todo = set(SalesRecord.objects.filter(customer__isnull=True).values_list("session_id", flat=True).distinct())
    todo |= set(GymMember.objects.filter(customer__isnull=True).values_list("session_id", flat=True).distinct())
    done = 0
    for session in Session.objects.filter(pk__in=todo).order_by("end_date", "created_at"):
        done += link_session(session)
    return done
