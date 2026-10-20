"""Which days of Expired Pack User Details are stored for which center.

Kronos only holds the last 90 days before today for this card, so only days inside that window
are recorded: an older day comes back empty and says nothing about whether it was ever stored.
"""
import datetime as dt

from django.utils import timezone

from .models import SalesCoverage, Session

WINDOW_DAYS = 90
ONE_DAY = dt.timedelta(days=1)


def window_start(today=None):
    return (today or timezone.localdate()) - dt.timedelta(days=WINDOW_DAYS)


def pieces(start, end, chunk):
    """The (first, last) day of each request for a period: whole, each day, or each week from the start."""
    if chunk == Session.WHOLE:
        return [(start, end)]
    step = dt.timedelta(days=1 if chunk == Session.DAY else 7)
    out, first = [], start
    while first <= end:
        out.append((first, min(first + step - ONE_DAY, end)))
        first += step
    return out


def days(first, last):
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]


def covered_days(center_id, start, end):
    return set(SalesCoverage.objects.filter(center_id=center_id, day__range=(start, end))
               .values_list("day", flat=True))


def to_fetch(session, center_id, today=None):
    """The pieces of this session's period still to ask for.

    Without fill_missing that is every piece. With it, a piece is skipped when all its days are stored,
    or when it lies entirely before the 90-day window (it would only come back empty).
    """
    today = today or timezone.localdate()
    all_pieces = pieces(session.start_date, session.end_date, session.sales_chunk)
    if not session.fill_missing:
        return all_pieces
    have = covered_days(center_id, session.start_date, session.end_date)
    oldest = window_start(today)
    return [(a, b) for a, b in all_pieces if b >= oldest and not all(d in have for d in days(a, b) if d >= oldest)]


def mark(session, center_id, fetched, today=None):
    """Record the days of the fetched pieces that are inside the window and already over."""
    today = today or timezone.localdate()
    oldest = window_start(today)
    rows = [SalesCoverage(center_id=center_id, day=d, session=session)
            for a, b in fetched for d in days(a, b) if oldest <= d < today]
    SalesCoverage.objects.bulk_create(rows, batch_size=500, update_conflicts=True,
                                      unique_fields=["center", "day"], update_fields=["session", "fetched_at"])


def center_ids(list_name):
    from centers import services
    return services.list_ids(list_name)


def window_days(today=None):
    """Every day of the 90-day window that is already over: the days that can be stored."""
    today = today or timezone.localdate()
    return days(window_start(today), today - ONE_DAY)


def missing_by_day(ids, today=None):
    """[{day, stored, missing}] for the days of the window, newest first, for these centers."""
    from django.db.models import Count
    stored = {}
    first = window_start(today)
    for i in range(0, len(ids), 900):
        for row in SalesCoverage.objects.filter(center_id__in=ids[i:i + 900], day__gte=first).values("day").annotate(n=Count("center_id")):
            stored[row["day"]] = stored.get(row["day"], 0) + row["n"]
    return [{"day": d, "stored": stored.get(d, 0), "missing": len(ids) - stored.get(d, 0)}
            for d in window_days(today)][::-1]


def missing_by_center(ids, today=None):
    """[{id, name, group, stored, missing}] for these centers, most missing first."""
    from django.db.models import Count

    from centers.models import Center, CenterGroup
    first, total = window_start(today), len(window_days(today))
    stored = {}
    for i in range(0, len(ids), 900):
        for row in SalesCoverage.objects.filter(center_id__in=ids[i:i + 900], day__gte=first).values("center_id").annotate(n=Count("day")):
            stored[row["center_id"]] = row["n"]
    members = {}
    for gname, cid in CenterGroup.centers.through.objects.order_by("centergroup__position").values_list("centergroup__name", "center_id"):
        members.setdefault(cid, []).append(gname)
    rows = [{"id": c.pk, "name": c.name, "group": ", ".join(members.get(c.pk) or []),
             "stored": stored.get(c.pk, 0), "missing": total - stored.get(c.pk, 0)}
            for c in Center.objects.filter(pk__in=ids)]
    rows.sort(key=lambda r: (-r["missing"], r["name"]))
    return rows


class CannotFetch(Exception):
    """Nothing was started; the message says why."""


def fetch_missing(list_name="actual", center_ids_=None, day=None, today=None):
    """Start a sales-only run, one request per day, that fetches just the days not stored yet.

    center_ids_: only these centers (default: the whole list). day: only that day (default: the whole window).
    Returns the Job. Raises CannotFetch when it cannot start (not logged in, another run is active, ...).
    """
    from . import auth, jobs
    from .conf import SPEED_PROFILES
    today = today or timezone.localdate()
    first, last = (day, day) if day else (window_start(today), today - ONE_DAY)
    if not auth.logged_in(fresh=True):
        raise CannotFetch("Not logged in to Kronos: log in on the Run page first.")
    spec = {"start": first.isoformat(), "end": last.isoformat(), "list": list_name,
            "center_ids": list(center_ids_) if center_ids_ else None, "only": ["sales"],
            "workers": SPEED_PROFILES["normal"], "speed": "normal", "granularity": Session.MONTH,
            "sales_chunk": Session.DAY, "fill_missing": True}
    try:
        return jobs.enqueue(spec)
    except jobs.JobError as e:
        raise CannotFetch(str(e))
