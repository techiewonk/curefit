"""Performance metrics as a wide table: one row per center and month, one column per metric.

The perf card stores one row per metric and date. Here those become columns, and several sessions that
cover the same month collapse to one figure: a session split by month wins over day/week splits, then the one
covering most of the month, then the newest. Only when a month has nothing but day or week rows are the
rows added up.
"""
import datetime as dt

from centers.models import Center, CenterGroup

from .analysis import revenue_metrics
from .models import PerfMetric, Session
from .revenue import label, month_of, next_month


def metric_names():
    return list(PerfMetric.objects.order_by().values_list("metric", flat=True).distinct().order_by("metric"))


def ordered_metrics(names):
    """Total / new / repeat revenue first, the rest alphabetically."""
    first = [m for m in revenue_metrics(names) if m]
    return first + [m for m in sorted(names) if m not in first]


def month_values(since=None, until=None, center_ids=None, metrics=None):
    """{(center_id, month): {metric: value}} for the months from `since` to `until` (first-of-month dates)."""
    qs = PerfMetric.objects.filter(metric_date__isnull=False, value__isnull=False)
    if since:
        qs = qs.filter(metric_date__gte=since)
    if until:
        qs = qs.filter(metric_date__lt=next_month(until))
    if center_ids is not None:
        qs = qs.filter(center_id__in=list(center_ids))
    if metrics:
        qs = qs.filter(metric__in=list(metrics))
    sums = {}  # (center, month, metric, session) -> [value, order]
    for cid, day, metric, value, sid, gran, began, ended, created in qs.values_list(
            "center_id", "metric_date", "metric", "value", "session_id", "session__granularity",
            "session__start_date", "session__end_date", "session__created_at"):
        month = month_of(day)
        overlap = (min(ended, next_month(month) - dt.timedelta(days=1)) - max(began, month)).days + 1
        entry = sums.setdefault((cid, month, metric, sid), [0.0, (gran == Session.MONTH, overlap, ended, created)])
        entry[0] += value
    best = {}
    for (cid, month, metric, _), (value, order) in sums.items():
        if (cid, month, metric) not in best or order > best[(cid, month, metric)][0]:
            best[(cid, month, metric)] = (order, value)
    out = {}
    for (cid, month, metric), (_, value) in best.items():
        out.setdefault((cid, month), {})[metric] = value
    return out


def group_members():
    members = {}
    for gname, cid in CenterGroup.centers.through.objects.order_by("centergroup__position", "centergroup__name") \
            .values_list("centergroup__name", "center_id"):
        members.setdefault(cid, []).append(gname)
    return members


def center_ids_for(group):
    """Center ids in a group; 'Ungrouped' = in none; '' = no restriction (None)."""
    if not group:
        return None
    members = group_members()
    if group == "Ungrouped":
        return [c for c in Center.objects.values_list("pk", flat=True) if c not in members]
    return [cid for cid, names in members.items() if group in names]


def wide_rows(group="", since=None, until=None, search="", metrics=None):
    """Rows for the wide table, newest month first; plus the metric columns that have any data."""
    ids = center_ids_for(group)
    values = month_values(since, until, ids, metrics)
    centers = {c.pk: c for c in Center.objects.filter(pk__in={cid for cid, _ in values})}
    members = group_members()
    needle = search.strip().lower()
    columns = ordered_metrics(sorted({m for v in values.values() for m in v}))
    rows = []
    for (cid, month), vals in values.items():
        c = centers.get(cid)
        if c is None or (needle and needle not in f"{c.name} {cid}".lower()):
            continue
        rows.append({"id": cid, "name": c.name, "group": ", ".join(members.get(cid) or ["Ungrouped"]), "month": month,
                     "cells": [vals.get(m) for m in columns]})
    rows.sort(key=lambda r: (-r["month"].toordinal(), r["name"]))
    return rows, columns


def table(rows, columns):
    headers = ["center_id", "center", "group", "month"] + columns
    return headers, [[r["id"], r["name"], r["group"], label(r["month"])] + r["cells"] for r in rows]


# ------------------------------------------------------------------ analysis
def _pct(new, old):
    return round(100.0 * (new - old) / old, 1) if new is not None and old else None


def parse_month(text, default=None):
    """'2026-08' (a month input) or '2026-08-01' -> first of that month; default when blank or wrong."""
    try:
        parts = str(text).split("-")
        return dt.date(int(parts[0]), int(parts[1]), 1)
    except (ValueError, IndexError):
        return default


def analyse(metric, group="", mode="mom", agg="sum", month=None, a=None, b=None, months_back=12, today=None):
    """Chart series and a comparison table for one metric.

    mode: mom (month vs previous), yoy (vs the same month last year), range (range A vs range B, each
    (first, last) month), group (each center vs the average of its group, for `month`).
    agg: sum or avg - how centers add up into a group line and total.
    """
    from django.utils import timezone
    today = today or timezone.localdate()
    current = month_of(today)
    month = month or month_of(current - dt.timedelta(days=1))  # default: the last full month
    ids = center_ids_for(group)
    start = (month_of(month - dt.timedelta(days=366)) if mode == "yoy"
             else min(a[0], b[0]) if mode == "range" and a and b else None)
    first_shown = current
    for _ in range(months_back - 1):
        first_shown = month_of(first_shown - dt.timedelta(days=1))
    since = min(filter(None, [first_shown, start, month_of(month - dt.timedelta(days=45))]))
    values = month_values(since, None, ids, [metric])
    members = group_members()
    centers = {c.pk: c for c in Center.objects.filter(pk__in={cid for cid, _ in values})}

    def val(cid, m):
        return values.get((cid, m), {}).get(metric)

    months = []
    m = first_shown
    while m <= current:
        months.append(m)
        m = next_month(m)
    lines = {}
    for g in [*dict.fromkeys(g for cid in {c for c, _ in values} for g in (members.get(cid) or ["Ungrouped"]))]:
        cids = [cid for cid in centers if g in (members.get(cid) or ["Ungrouped"])]
        points = []
        for mm in months:
            got = [val(cid, mm) for cid in cids if val(cid, mm) is not None]
            points.append((sum(got) if agg == "sum" else sum(got) / len(got)) if got else None)
        lines[g] = points

    def rng(r):
        out, mm = [], r[0]
        while mm <= r[1]:
            out.append(mm)
            mm = next_month(mm)
        return out

    rows = []
    for cid, c in centers.items():
        if mode == "mom":
            now, before = val(cid, month), val(cid, month_of(month - dt.timedelta(days=1)))
        elif mode == "yoy":
            now, before = val(cid, month), val(cid, month.replace(year=month.year - 1))
        elif mode == "range" and a and b:
            xs = [val(cid, mm) for mm in rng(a) if val(cid, mm) is not None]
            ys = [val(cid, mm) for mm in rng(b) if val(cid, mm) is not None]
            now = (sum(xs) if agg == "sum" else sum(xs) / len(xs)) if xs else None
            before = (sum(ys) if agg == "sum" else sum(ys) / len(ys)) if ys else None
        else:  # group
            now, before = val(cid, month), None
        rows.append({"id": cid, "name": c.name, "group": ", ".join(members.get(cid) or ["Ungrouped"]),
                     "groups": members.get(cid) or ["Ungrouped"], "now": now, "before": before,
                     "delta": None if now is None or before is None else now - before, "pct": _pct(now, before)})
    if mode == "group":
        averages = {}
        for r in rows:
            for g in r["groups"]:
                averages.setdefault(g, []).append(r["now"])
        for g, xs in list(averages.items()):
            xs = [x for x in xs if x is not None]
            averages[g] = sum(xs) / len(xs) if xs else None
        for r in rows:
            r["before"] = averages.get(r["groups"][0])  # the group average, in the 'before' column
            r["delta"] = None if r["now"] is None or r["before"] is None else r["now"] - r["before"]
            r["pct"] = _pct(r["now"], r["before"])
    rows.sort(key=lambda r: (r["now"] is None, -(r["now"] or 0), r["name"]))
    done = [r["now"] for r in rows if r["now"] is not None]
    before_done = [r["before"] for r in rows if r["before"] is not None and mode != "group"]
    total_now = (sum(done) if agg == "sum" else sum(done) / len(done)) if done else None
    total_before = (sum(before_done) if agg == "sum" else sum(before_done) / len(before_done)) if before_done else None
    return {"months": months, "lines": lines, "rows": rows, "month": month, "mode": mode, "agg": agg,
            "total_now": total_now, "total_before": total_before, "total_pct": _pct(total_now, total_before)}
