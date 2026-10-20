"""Read-side queries for the admin pages: what to show, never how to show it."""
from datetime import timedelta

from django.db.models import Avg, Count, Q, Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince

from centers.models import Center, CenterGroup, CenterList, KnownEmpty

from . import analysis, auth
from .models import AutoRun, Job, SalesRecord, SectionResult, Session
from .reports import REPORTS

PAIR_OVERHEAD = 2.0   # average polite delay between two requests (seconds)
UNKNOWN_SECONDS = 40.0  # assumed for a report nobody has timed yet
HIGH_FACTOR = 1.5     # slow cards make the real time drift upwards


# ------------------------------------------------------------------ run page
def estimate(center_count, reports, workers, sales_pieces=1):
    """How long a run is expected to take, from the sections timed in earlier sessions.

    sales_pieces: how many day / week requests the sales report makes per center (1 = the whole period)."""
    measured = {r["report"]: r["avg"] for r in SectionResult.objects
                .filter(status=SectionResult.OK, seconds__isnull=False).values("report")
                .annotate(avg=Avg("seconds"), n=Count("id")).filter(n__gte=3)}
    per_center = sum((measured[r] + PAIR_OVERHEAD if r in measured else UNKNOWN_SECONDS)
                     * (sales_pieces if r == "sales" else 1) for r in reports)
    low = per_center * center_count / workers
    return {"centers": center_count, "reports": len(reports), "sales_pieces": sales_pieces,
            "pairs": center_count * (len(reports) - ("sales" in reports) + (sales_pieces if "sales" in reports else 0)),
            "workers": workers, "low_text": _duration(low), "high_text": _duration(low * HIGH_FACTOR),
            "seconds_low": round(low), "seconds_high": round(low * HIGH_FACTOR),
            "assumed": sorted(set(reports) - set(measured))}


def _duration(seconds):
    """'2 hours, 5 minutes' (Django's own timesince does the wording)."""
    now = timezone.now()
    return timesince(now - timedelta(seconds=max(seconds, 60)), now) if seconds >= 60 else "under a minute"


SALES_WINDOW_DAYS = 90  # the "Expired Pack User Details" card only holds expiries of the last 90 days


def sales_window_notice(end, reports):
    """Warning text when the sales report can only come back empty for this period, else None."""
    since = timezone.localdate() - timedelta(days=SALES_WINDOW_DAYS)
    if "sales" in reports and end and end < since:
        return (f"The sales report only holds the last {SALES_WINDOW_DAYS} days (since {since:%d %b %Y}): "
                "for a period that ends earlier it comes back empty, and asking costs about a minute per center. "
                "Untick it, or pick a more recent period.")
    return None


# ------------------------------------------------------------------ monitor
CELL = {  # state -> (Tailwind colour from Unfold's stylesheet, label)
    SectionResult.OK: ("bg-green-500", "ok"),
    SectionResult.EMPTY: ("bg-blue-500", "empty"),
    SectionResult.FAILED: ("bg-red-500", "failed"),
    SectionResult.SLOW: ("bg-orange-500", "slow (timed out)"),
    SectionResult.SKIPPED: ("bg-base-400", "skipped"),
    "running": ("bg-primary-600 animate-pulse", "running"),
    "waiting": (None, "waiting"),
}


def monitor(session, job=None):
    """Centers x reports grid for one session, ready for the tracker component."""
    ids = [int(c) for c in session.center_ids]
    names = dict(Center.objects.filter(center_id__in=ids).values_list("center_id", "name"))
    found = {(c, r): (st, n) for c, r, st, n in SectionResult.objects.filter(session=session)
             .values_list("center_id", "report", "status", "row_count")}
    live = bool(job and job.session_id == session.pk and job.is_active)
    active = set(job.active) if live else set()
    results_url = reverse("admin:scraper_sectionresult_changelist")
    counts = dict.fromkeys(CELL, 0)
    rows = []
    for cid in ids:
        cells = []
        for report in session.reports:
            hit = found.get((cid, report))
            state, n = hit if hit else ("running" if f"{cid}|{report}" in active else "waiting", None)
            color, label = CELL[state]
            counts[state] += 1
            cells.append({
                "color": color,
                "tooltip": f"{names.get(cid, '')} · {report}: {label}" + (f" ({n} rows)" if n else ""),
                # a finished section links to its row in the admin (error text, row count...)
                "href": f"{results_url}?session__session_id__exact={session.pk}"
                        f"&center__center_id__exact={cid}&report={report}" if hit else None,
            })
        rows.append({"center_id": cid, "name": names.get(cid, ""), "cells": cells})
    legend = [{"label": CELL[s][1], "color": CELL[s][0] or "bg-base-300", "count": counts[s]} for s in CELL]
    return {"session": session, "rows": rows, "legend": legend, "reports": list(session.reports),
            "live": live, "job": job if live else None}


# ------------------------------------------------------------------ analytics / compare
def analytics(session):
    """Per-center analysis, totals and pack mix for a session."""
    result = analysis.build_analysis(session)
    rows = result["rows"]
    sales = {r["center_id"]: r for r in SalesRecord.objects.filter(session=session).values("center_id")
             .annotate(n=Count("id"), active=Count("id", filter=Q(active_last_30d="Yes")), revenue=Sum("revenue"))}
    packs = list(SalesRecord.objects.filter(session=session).values("pack_name")
                 .annotate(sales=Count("id"), revenue=Sum("revenue")).order_by("-sales"))
    sales_count, sales_revenue = sum(r[2] or 0 for r in rows), sum(r[3] or 0 for r in rows)
    active = sum(r["active"] for r in sales.values())
    centers = [{"id": r[0], "name": r[1], "sales": r[2], "sales_revenue": r[3], "renewals": r[4],
                "renewal_pct": r[5], "total_revenue": r[6], "new_revenue": r[7], "repeat_revenue": r[8],
                "new_pct": r[9], "repeat_pct": r[10],
                "active_pct": round(100 * sales[r[0]]["active"] / sales[r[0]]["n"], 1) if r[0] in sales else None}
               for r in rows]
    return {"centers": centers, "packs": packs, "rows_pruned": session.rows_pruned,
            "totals": {"centers": len(rows), "sales_count": sales_count, "sales_revenue": sales_revenue,
                       "total_revenue": sum(r[6] or 0 for r in rows), "new_revenue": sum(r[7] or 0 for r in rows),
                       "repeat_revenue": sum(r[8] or 0 for r in rows),
                       "avg_ticket": round(sales_revenue / sales_count) if sales_count else 0,
                       "active_rate": round(100 * active / sales_count, 1) if sales_count else 0}}


def compare(a, b):
    """Per-center sales and revenue in two sessions, with the change from a to b.

    A gym found in only one session (`one_sided`) has no a or b figures; its change counts the missing side as 0, so
    the change totals include gyms that were added or dropped."""
    merged = {}
    for tag, session in (("a", a), ("b", b)):
        for r in analysis.build_analysis(session)["rows"]:
            merged.setdefault(r[0], {"id": r[0], "name": r[1]})[tag] = {
                "sales": r[2], "sales_revenue": r[3], "total_revenue": r[6]}
    rows = []
    for cid in sorted(merged):
        row = merged[cid]
        row["one_sided"] = not ("a" in row and "b" in row)
        mine, theirs = row.get("a") or {}, row.get("b") or {}
        row["diff"] = {k: _change(mine.get(k), theirs.get(k), k) for k in COMPARE_KEYS}
        rows.append(row)
    return rows


COMPARE_KEYS = ("sales", "sales_revenue", "total_revenue")
PRUNED_HELP = ("Session {} had its per-run sales rows deleted to save space (Settings > Keep run rows), so its sales "
               "show as 0 here; the all-runs tables still hold them.")


def _change(a, b, key):
    change = (b or 0) - (a or 0)
    return change if key == "sales" else round(change, 2)


def compare_totals(rows):
    """Totals of a comparison over every gym, including gyms found in only one of the two sessions:
    {key: {"a": total, "b": total, "change": b - a}} for sales, sales_revenue and total_revenue."""
    out = {}
    for k in COMPARE_KEYS:
        a = sum(((r.get("a") or {}).get(k) or 0) for r in rows)
        b = sum(((r.get("b") or {}).get(k) or 0) for r in rows)
        out[k] = {"a": a, "b": b, "change": _change(a, b, k)}
    return out


def compare_notices(a, b):
    """Why a comparison may mislead: a session whose sales rows were pruned shows no sales."""
    return [PRUNED_HELP.format(s.session_id) for s in (a, b) if s.rows_pruned]


# ------------------------------------------------------------------ home
RECENT_DAYS = 7  # failures older than this are no longer 'needs attention'


def _kronos_state():
    """'ok', 'logged_out' or 'unreachable' (auth.state() when it exists, else the plain yes/no of logged_in)."""
    state = getattr(auth, "state", None)
    if not callable(state):
        return "ok" if auth.logged_in() else "logged_out"
    answer = state()
    if answer == getattr(auth, "OK", "ok"):
        return "ok"
    return "unreachable" if answer == getattr(auth, "UNREACHABLE", "unreachable") else "logged_out"


def _recent_failures(since, latest_job, shown_session=None):
    """Needs-attention items for failed jobs, failed job-runner tasks and failed post-processing since `since`
    (`shown_session`'s findings are already listed by the caller)."""
    alerts = []
    errors = Job.objects.filter(state=Job.ERROR, created_at__gte=since)
    if latest_job:
        errors = errors.exclude(pk=latest_job.pk)  # the latest job has its own alert
    n = errors.count()
    if n:
        alerts.append({"level": "danger", "url": "admin:scraper_job_changelist",
                       "text": f"{n} other job(s) ended in error in the last {RECENT_DAYS} days; "
                               f"latest: {errors.first().error or errors.first().pk}."})
    try:
        from django_q.models import Failure
        failed = Failure.objects.filter(stopped__gte=since)
        n = failed.count()
        if n:
            alerts.append({"level": "danger", "url": "admin:django_q_failure_changelist",
                           "text": f"{n} job-runner task(s) failed in the last {RECENT_DAYS} days; "
                                   f"latest: {failed.order_by('-stopped').first().name}."})
    except Exception:  # noqa: BLE001 - the job runner's tables are optional for this page
        pass
    for s in Session.objects.filter(created_at__gte=since).exclude(pk=getattr(shown_session, "pk", None)) \
            .only("session_id", "checks"):
        for finding in s.checks or []:
            if _post_processing(finding):
                alerts.append({**finding, "level": "danger", "text": f"Session {s.session_id}: {finding.get('text', '')}"})
    return alerts


def _post_processing(finding):
    """A finding that says an after-run step failed: checks.post_processing_error(), or an engine step that failed
    (it carries the step name and level 'error')."""
    return isinstance(finding, dict) and (finding.get("kind") == "post_processing"
                                          or (finding.get("step") and finding.get("level") in ("error", "danger")))


def _level(finding):
    """The page knows danger / warning / info: an 'error' finding is shown as danger."""
    level = finding.get("level") or "info"
    return "danger" if level == "error" else level


def home():
    """Everything the Home dashboard shows."""
    job = Job.objects.first()
    alerts = []
    if job and job.state in (Job.ERROR, Job.INTERRUPTED):
        alerts.append({"level": "danger", "text": job.error or f"The last job ended as {job.state}.",
                       "url": "console:run"})
    covered = Session.objects.exclude(coverage__isnull=True).first()
    alerts += _recent_failures(timezone.now() - timedelta(days=RECENT_DAYS), job, covered)
    kronos = _kronos_state()
    if kronos == "unreachable":
        alerts.append({"level": "warning", "url": "console:run",
                       "text": "Cannot reach Kronos (network or Kronos down); the login itself may still be fine."})
    elif kronos != "ok":
        alerts.append({"level": "warning", "text": "Not logged in to Kronos - a scrape needs a fresh OTP.",
                       "url": "console:run"})
    unfinished = Session.objects.unfinished()
    if unfinished.exists():
        latest = unfinished.first()
        alerts.append({"level": "warning", "url": "console:run",
                       "text": f"{unfinished.count()} unfinished session(s); latest {latest.session_id} "
                               f"({latest.start_date} to {latest.end_date}) is missing "
                               f"{latest.missing_count} center/section pairs."})
    if covered:
        alerts += [{**finding, "level": _level(finding), "text": f"Session {covered.session_id}: {finding.get('text', '')}"}
                   for finding in covered.checks or [] if isinstance(finding, dict)]
        cov = [x for x in covered.coverage or [] if isinstance(x, dict)]
        for key, text, level in (("failed", "failed center/section pairs", "danger"),
                                 ("slow", "slow (timed-out) sections", "warning"),
                                 ("possibly_truncated", "results that hit the row cap", "warning")):
            n = sum(x.get(key) or 0 for x in cov)
            if n:
                alerts.append({"level": level, "text": f"Session {covered.session_id}: {n} {text}."})
        skipped = [x.get("report", "?") for x in cov if x.get("skipped_after_failures")]
        if skipped:
            alerts.append({"level": "danger", "text": f"Reports skipped after repeated failures: {', '.join(skipped)}."})
        empty = sum(x.get("empty") or 0 for x in cov)
        if empty:
            alerts.append({"level": "info", "text": f"Session {covered.session_id}: {empty} empty sections "
                                                    f"({KnownEmpty.objects.count()} registered as known-empty)."})
    from . import autorun  # here, not at the top: autorun imports the engine
    autoruns = list(AutoRun.objects.select_related("centers", "last_session"))
    for a in autoruns:
        a.next_at = autorun.next_run(a)
        if a.enabled and a.last_status in ("failed", "skipped", "incomplete"):
            alerts.append({"level": "warning", "text": f"Automatic run '{a.name}': {a.last_message}",
                           "url": "console:run"})
    from . import revenue  # also late: revenue reads the perf tables
    group_cards = revenue.matrix(months=3, stored=True)["summary"]  # once, from the stored monthly table
    latest = Session.objects.filter(status=Session.COMPLETE).first() or Session.objects.first()
    return {"job": job, "alerts": alerts, "autoruns": autoruns, "group_cards": group_cards,
            "groups_defined": CenterGroup.objects.count(), "sessions": Session.objects.all()[:6],
            "latest": latest,
            "kpis": analytics(latest)["totals"] if latest and SalesRecord.objects.filter(session=latest).exists() else None,
            "lists": CenterList.objects.all(), "active_centers": Center.objects.filter(state="ACTIVE").count(),
            "logged_in": auth.logged_in()}
