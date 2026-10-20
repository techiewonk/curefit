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
