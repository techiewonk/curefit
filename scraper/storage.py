"""Saving one center/report result: data rows + the SectionResult, together or not at all."""
import datetime

from django.db import transaction
from django.utils import timezone

from .models import CardColumns, CardRow, GymMember, PerfMetric, SalesRecord, SectionResult


def _f(value):
    try:
        return None if value is None or value == "" else round(float(value), 4)  # no float noise in the database
    except (TypeError, ValueError):
        return None


def _day(value):
    """A date from Metabase text ('2026-06-11T00:00:00Z' or '2026-06-11'); None when there is none."""
    try:
        return datetime.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _typed_rows(report, session, center_id, rows, now):
    base = {"session": session, "center_id": center_id, "scraped_at": now}
    if report == "sales":  # [user_id, name, phone, pack, end, active, renewed, revenue]
        return SalesRecord, [SalesRecord(
            **base, user_id=str(r[0]), name=r[1], phone=r[2], pack_name=r[3], member_end_date=_day(r[4]),
            active_last_30d=r[5], renewed=r[6], revenue=_f(r[7])) for r in rows]
    if report == "perf":  # [date, metric, value]
        return PerfMetric, [PerfMetric(
            **base, metric_date=_day(r[0]), metric=(r[1] or "").strip(), value=_f(r[2])) for r in rows]
    if report == "gym":  # [userid, username, pack, amount, purchased, start, end, status, ...]
        return GymMember, [GymMember(
            **base, user_id=str(r[0]), username=r[1], pack_name=r[2], final_amount_paid=_f(r[3]),
            purchase_date=_day(r[4]), pack_start_date=_day(r[5]), pack_end_date=_day(r[6]), status=r[7],
            offline_vs_online=r[8], new_vs_repeat=r[9], class_level=r[10],
            last_class_date=_day(r[11])) for r in rows]
    return None, None


@transaction.atomic
def save_section(session, center_id, report, rows, cols, seconds=None, now=None):
    """Replace this session's rows for the center/report with `rows` and record the result."""
    now = now or timezone.now()
    model, objs = _typed_rows(report, session, center_id, rows, now)
    if model is not None:
        model.objects.filter(session=session, center_id=center_id).delete()
        model.objects.bulk_create(objs, batch_size=1000)
    else:
        CardRow.objects.filter(session=session, center_id=center_id, report=report).delete()
        CardRow.objects.bulk_create(
            [CardRow(session=session, center_id=center_id, scraped_at=now, report=report,
                     row_num=i, data=list(r)) for i, r in enumerate(rows)], batch_size=1000)
        CardColumns.objects.update_or_create(report=report, defaults={"columns": cols})
    record_result(session, center_id, report, SectionResult.OK, len(rows), seconds, now=now)


def clear_rows(session, center_id, report):
    """Forget what this session saved for a center and report (a run that starts the center over)."""
    model, _ = _typed_rows(report, session, center_id, [], timezone.now())
    if model is not None:
        model.objects.filter(session=session, center_id=center_id).delete()
    else:
        CardRow.objects.filter(session=session, center_id=center_id, report=report).delete()


def append_rows(session, center_id, report, rows, now=None):
    """Add rows to what the session has for a center and report (a piece of a day-by-day fetch). Nothing is deleted."""
    now = now or timezone.now()
    model, objs = _typed_rows(report, session, center_id, rows, now)
    if model is not None and objs:
        model.objects.bulk_create(objs, batch_size=1000)
    return len(objs or [])


def record_result(session, center_id, report, status, row_count=0, seconds=None, error="", now=None):
    SectionResult.objects.update_or_create(
        session=session, center_id=center_id, report=report,
        defaults={"status": status, "row_count": row_count, "seconds": seconds,
                  "error": error[:2000], "scraped_at": now or timezone.now()})
