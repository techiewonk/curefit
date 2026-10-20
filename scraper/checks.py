"""Automatic sanity checks on a finished session: things worth a human look."""
from django.db.models import Count

from centers.models import Center

from .models import SalesRecord, Session

CHANGE_RATIO = 0.3   # a center whose sales rows moved by more than 30% since the last run of the period
CHANGE_MIN = 20      # ...and by at least this many rows
SHOW = 5             # centers named per finding


def previous_like(session):
    """The last complete session that can be compared with this one: same period, split and sales chunk, a full
    fetch (not 'only missing days'), and its sales rows still there (not pruned)."""
    return Session.objects.for_period(session.start_date, session.end_date, session.granularity) \
        .filter(created_at__lt=session.created_at, status=Session.COMPLETE, sales_chunk=session.sales_chunk,
                fill_missing=False, rows_pruned=False).first()


def run(session):
    """Findings as [{"level": "warning"|"info", "text": ...}], newest-first-worthy ones first."""
    findings = []
    sales = dict(SalesRecord.objects.filter(session=session).values_list("center_id")
                 .annotate(n=Count("id")))

    # a 'missing days only' run fetches just the gaps, so few or no rows for a center is expected there
    if "sales" in session.reports and sales and not session.fill_missing:
        silent = Center.objects.filter(center_id__in=session.center_ids, state="ACTIVE") \
            .exclude(center_id__in=sales).order_by("center_id")
        if silent.exists():
            findings.append({"level": "warning", "text": _named(
                f"{silent.count()} ACTIVE center(s) returned no sales rows", silent)})

    previous = previous_like(session) if sales and not session.fill_missing else None
    if previous:
        both = {int(c) for c in session.center_ids} & {int(c) for c in previous.center_ids}
        before = dict(SalesRecord.objects.filter(session=previous, center_id__in=both).values_list("center_id")
                      .annotate(n=Count("id")))
        moved = sorted(((c, before[c], n) for c, n in sales.items() if c in before
                        and abs(n - before[c]) >= CHANGE_MIN and abs(n - before[c]) > CHANGE_RATIO * before[c]),
                       key=lambda x: -abs(x[2] - x[1]))
        if moved:
            text = ", ".join(f"{c} ({b} → {n})" for c, b, n in moved[:SHOW])
            findings.append({"level": "info", "text": f"{len(moved)} center(s) changed a lot since session "
                                                      f"{previous.pk}: {text}{'…' if len(moved) > SHOW else ''}"})

    for item in session.coverage or []:
        if isinstance(item, dict) and item.get("possibly_truncated"):
            findings.append({"level": "warning", "text": f"{item.get('report', '?')}: {item['possibly_truncated']} "
                                                         "result(s) hit the row cap and may be cut off"})
    return findings


def post_processing_error(error):
    """The finding to add to session.checks when linking customers / merging / the monthly table failed after a
    run: Home lists it under Needs attention (kind 'post_processing')."""
    return {"level": "danger", "kind": "post_processing",
            "text": f"Post-processing failed (customers, all-runs tables or monthly table not updated): {error}. "
                    "Run link_customers, merge_masters and merge_months to retry."}


def _named(text, centers):
    names = ", ".join(f"{c.center_id} {c.name}" for c in centers[:SHOW])
    return f"{text}: {names}{'…' if centers.count() > SHOW else ''}"
