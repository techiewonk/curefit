"""The all-runs tables: My Gym Member and Expired Pack User Details merged into one row per member pack.

A run adds packs it has not seen and updates the ones it has. The data of the newest *scrape* wins
(by `scraped_at`, so merging an old session late cannot undo a newer one), and a date never moves
backwards: the later of the stored and the new date is kept.
"""
from .customers import _in, clean_user_id, normalise_phone
from .models import GymPack, GymMember, SalesPack, SalesRecord, Session

BATCH = 1000

# model, session-row model, key fields, fields copied from the newest scrape, dates that only move forward
SPECS = {
    "gym": dict(
        master=GymPack, rows=GymMember,
        key=("center_id", "user_id", "pack_name", "pack_start_date"),  # an extension keeps the same pack
        copy=("username", "final_amount_paid", "purchase_date", "status", "offline_vs_online", "new_vs_repeat",
              "class_level"),
        amount="final_amount_paid",
        forward=("pack_end_date", "last_class_date")),
    "sales": dict(
        master=SalesPack, rows=SalesRecord,
        key=("center_id", "user_id", "pack_name", "member_end_date"),
        copy=("name", "phone", "active_last_30d", "renewed", "revenue"),
        amount="revenue",
        forward=()),
}


def make_key(row, fields):
    return "|".join("" if row[f] is None else str(row[f]) for f in fields)


def _identity(row):
    """Who a row is about when Kronos gave no user id: the phone, else the name; None when neither is there.
    Without this, every no-id row of a center, pack and date would collapse into one person."""
    phone = normalise_phone(row.get("phone"))
    if phone:
        return f"~phone:{phone}"
    name = " ".join(str(row.get("name") or row.get("username") or "").lower().split())
    return f"~name:{name[:60]}" if name else None


def row_key(row, fields, anonymous=None):
    """The all-runs table key of a session row: the user id, else `_identity`, else the row's place among the
    nameless rows of its pack (`anonymous`, set by `_keys`): stable when a later run returns the same rows."""
    if row["user_id"]:
        return make_key(row, fields)
    who = _identity(row) or f"~anon:{anonymous if anonymous is not None else row['id']}"
    return make_key({**row, "user_id": who}, fields)


def _keys(rows, spec):
    """[(key, row)] for a session's rows. Rows with no user id, phone or name cannot be told apart, so each is kept
    as its own pack, numbered within its center / pack / date by amount (the same rows next run get the same keys)."""
    amount, out, nameless = spec["amount"], [], {}
    for row in rows:
        if row["user_id"] or _identity(row):
            out.append((row_key(row, spec["key"])[:200], row))
        else:
            nameless.setdefault(make_key(row, spec["key"]), []).append(row)
    for group in nameless.values():
        group.sort(key=lambda r: (r[amount] if r[amount] is not None else float("-inf"), r["id"]))
        out += [(row_key(row, spec["key"], n)[:200], row) for n, row in enumerate(group, 1)]
    return out


def _later(old, new):
    return new if old is None or (new is not None and new > old) else old


def _combine(lines, spec):
    """One row from the payment lines of a pack (installments, refunds, ...): the amounts are added up and the
    other fields come from the largest line (then the newest), so the result does not depend on row order."""
    amount = spec["amount"]
    primary = max(lines, key=lambda r: (r[amount] if r[amount] is not None else float("-inf"), r["scraped_at"], r["id"]))
    row = dict(primary)
    amounts = [r[amount] for r in lines if r[amount] is not None]
    row[amount] = round(sum(amounts), 4) if amounts else None
    row["scraped_at"] = max(r["scraped_at"] for r in lines)
    for f in spec["forward"]:
        row[f] = max((r[f] for r in lines if r[f] is not None), default=None)
    row["customer_id"] = next((r["customer_id"] for r in lines if r["customer_id"]), None)
    return row


def _merge(session, spec):
    fields = tuple(dict.fromkeys(spec["key"] + spec["copy"] + spec["forward"] + ("customer_id", "scraped_at")))
    grouped, rows = {}, []
    for row in spec["rows"].objects.filter(session=session).order_by("scraped_at", "id").values("id", *fields):
        row["user_id"] = clean_user_id(row["user_id"]) or ""
        rows.append(row)
    for key, row in _keys(rows, spec):
        grouped.setdefault(key, []).append(row)
    incoming = {key: _combine(lines, spec) for key, lines in grouped.items()}
    master = spec["master"]
    have = {m.key: m for m in _in(master.objects, "key", list(incoming))}
    new, changed = [], []
    for key, row in incoming.items():
        mine = have.get(key)
        if mine is None:
            new.append(master(key=key, last_session=session, **{f: row[f] for f in fields}))
            continue
        if row["scraped_at"] >= mine.scraped_at:  # newest scrape wins
            for f in spec["copy"]:
                setattr(mine, f, row[f])
            mine.scraped_at, mine.last_session = row["scraped_at"], session
        else:  # an older scrape arriving late only fills blanks
            for f in spec["copy"]:
                if getattr(mine, f) in (None, ""):
                    setattr(mine, f, row[f])
        for f in spec["forward"]:
            setattr(mine, f, _later(getattr(mine, f), row[f]))
        mine.customer_id = row["customer_id"] or mine.customer_id
        changed.append(mine)
    master.objects.bulk_create(new, batch_size=BATCH, ignore_conflicts=True)
    master.objects.bulk_update(
        changed, list(spec["copy"]) + list(spec["forward"]) + ["customer", "scraped_at", "last_session"],
        batch_size=BATCH)
    return len(new), len(changed)


def merge_session(session):
    """Merge one session's gym and sales rows into the all-runs tables. Safe to repeat."""
    session = Session.objects.get(pk=session.pk)
    out = {name: _merge(session, spec) for name, spec in SPECS.items()}
    Session.objects.filter(pk=session.pk).update(merged_into_masters=True)
    return out


def prune_run_rows(keep):
    """Delete the per-run sales and gym rows of all but the newest `keep` sessions (0 = keep everything).

    The all-runs tables keep that data. Performance numbers and the other cards are not touched. A session that
    lost rows is marked `rows_pruned`, so its Analytics / Compare can say why it shows no sales and the checks do
    not compare against it.
    """
    if not keep:
        return 0
    newest = list(Session.objects.order_by("-created_at").values_list("pk", flat=True)[:keep])
    old = list(Session.objects.exclude(pk__in=newest).filter(merged_into_masters=True).values_list("pk", flat=True))
    deleted = 0
    for i in range(0, len(old), 500):
        chunk = old[i:i + 500]
        hit = set(SalesRecord.objects.filter(session_id__in=chunk).values_list("session_id", flat=True).distinct()) | \
            set(GymMember.objects.filter(session_id__in=chunk).values_list("session_id", flat=True).distinct())
        deleted += SalesRecord.objects.filter(session_id__in=chunk).delete()[0]
        deleted += GymMember.objects.filter(session_id__in=chunk).delete()[0]
        Session.objects.filter(pk__in=hit).update(rows_pruned=True)
    return deleted


def merge_all():
    """Every session, oldest first (used once to fill the tables from data scraped earlier)."""
    out = {}
    for session in Session.objects.order_by("end_date", "created_at"):
        out[session.session_id] = merge_session(session)
    return out
