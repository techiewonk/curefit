"""Revenue by gym and month: the management view (gyms x months, average, ranking, growth, target).

Source: the 'Total Revenue' metric of the Franchise Performance card, split by month. Several sessions can
cover the same month; for each center and month the newest session's numbers are used, so a re-run
corrects the figure instead of adding to it.
"""
import datetime as dt

from django.utils import timezone

from centers.models import Center, CenterGroup

from .analysis import revenue_metrics
from .models import PerfMetric

UNGROUPED = "Ungrouped"


def month_of(day):
    return day.replace(day=1)


def label(month):
    return month.strftime("%b-%y")


def next_month(month):
    return (month + dt.timedelta(days=32)).replace(day=1)


def _months(current, count):
    out, m = [], current
    for _ in range(count):
        out.append(m)
        m = month_of(m - dt.timedelta(days=1))
    return out[::-1]


def _ranks(values):
    """{key: 1-based rank}, highest value first; keys with no value are not ranked."""
    ordered = sorted((k for k, v in values.items() if v is not None), key=lambda k: -values[k])
    return {k: i + 1 for i, k in enumerate(ordered)}


ALL_MONTHS, MOST_MONTHS = 0, 48


def matrix(group="", months=12, today=None, since=None, until=None):
    """months: how many to show ending with the current one; 0 = from the first month that has data.

    since / until (first-of-month dates) narrow the months shown."""
    today = today or timezone.localdate()
    current = month_of(today)
    names = PerfMetric.objects.order_by().values_list("metric", flat=True).distinct()
    total_metric = revenue_metrics(list(names))[0]
    if months == ALL_MONTHS:
        first = PerfMetric.objects.filter(metric=total_metric, metric_date__isnull=False).order_by("metric_date") \
            .values_list("metric_date", flat=True).first() if total_metric else None
        months = 1 if first is None else min(MOST_MONTHS, (current.year - first.year) * 12 + current.month - first.month + 1)
    shown = _months(current, months)
    narrowed = [m for m in shown if (not since or m >= since) and (not until or m <= until)]
    shown = narrowed or shown
    complete = [m for m in shown if m < current]
    latest_month = complete[-1] if complete else None
    prev_month = complete[-2] if len(complete) > 1 else None

    members = {}  # center id -> names of the groups it is in
    for gname, cid in CenterGroup.centers.through.objects.order_by("centergroup__position", "centergroup__name") \
            .values_list("centergroup__name", "center_id"):
        members.setdefault(cid, []).append(gname)

    best = {}  # (center, month) -> (session rank, value)
    if total_metric:
        qs = PerfMetric.objects.filter(metric=total_metric, metric_date__gte=shown[0], value__isnull=False)
        if group == UNGROUPED:
            qs = qs.exclude(center_id__in=list(members))
        elif group:
            qs = qs.filter(center_id__in=[cid for cid, names_ in members.items() if group in names_])
        sums = {}
        for cid, day, value, sid, began, ended, created in qs.values_list(
                "center_id", "metric_date", "value", "session_id", "session__start_date", "session__end_date",
                "session__created_at"):
            month = month_of(day)
            # prefer the session that covers most of the month, then the newest: a later run for a few
            # days of a month must not replace a full-month figure
            overlap = (min(ended, next_month(month) - dt.timedelta(days=1)) - max(began, month)).days + 1
            sums.setdefault((cid, month, sid), [0.0, (overlap, ended, created)])[0] += value
        for (cid, month, _), (value, order) in sums.items():
            if (cid, month) not in best or order > best[(cid, month)][0]:
                best[(cid, month)] = (order, value)

    centers = {c.pk: c for c in Center.objects.filter(pk__in={cid for cid, _ in best})}
    rows = []
    for cid, c in centers.items():
        values = {m: best[(cid, m)][1] for m in shown if (cid, m) in best}
        done = [values[m] for m in complete if m in values]
        latest, prev = values.get(latest_month), values.get(prev_month)
        rows.append({
            "id": cid, "name": c.name, "note": c.note, "groups": members.get(cid) or [UNGROUPED],
            "group": ", ".join(members.get(cid) or [UNGROUPED]), "values": values,
            "average": sum(done) / len(done) if done else None, "latest": latest,
            "mom_pct": round(100.0 * (latest - prev) / prev, 1) if latest is not None and prev else None,
            "target": c.monthly_target,
            "achievement": round(100.0 * latest / c.monthly_target, 1) if latest is not None and c.monthly_target else None})
    rows.sort(key=lambda r: r["name"])

    # one table per group, like the sheet; ranks are within the group (a gym in two groups ranks in each)
    order = list(CenterGroup.objects.values_list("name", flat=True)) + [UNGROUPED]
    sections = []
    rank_months = shown[-6:]  # a ranking column for each of the last six months (the current one is partial)
    for name in order:
        mine = [dict(r) for r in rows if name in r["groups"]]
        if not mine:
            continue
        rank_latest = _ranks({r["id"]: r["latest"] for r in mine})
        rank_avg = _ranks({r["id"]: r["average"] for r in mine})
        by_month = {m: _ranks({r["id"]: r["values"].get(m) for r in mine}) for m in rank_months}
        for r in mine:
            r["rank_latest"], r["rank_average"] = rank_latest.get(r["id"]), rank_avg.get(r["id"])
            r["ranks"] = [by_month[m].get(r["id"]) for m in rank_months]
        mine.sort(key=lambda r: (r["rank_latest"] is None, r["rank_latest"] or 0, r["name"]))
        sections.append({"group": name, "rows": mine})

    group_totals = {}  # a center in two groups counts in both
    for r in rows:
        for g in r["groups"]:
            series = group_totals.setdefault(g, {m: 0.0 for m in shown})
            for m, v in r["values"].items():
                series[m] += v
    summary = []
    for name, series in sorted(group_totals.items(), key=lambda kv: -(kv[1].get(latest_month) or 0)):
        mine = [r for r in rows if name in r["groups"]]
        done = [series[m] for m in complete if series[m]]
        latest, prev = series.get(latest_month), series.get(prev_month)
        targets = sum(r["target"] or 0 for r in mine)
        summary.append({
            "group": name, "gyms": len(mine), "latest": latest, "average": sum(done) / len(done) if done else None,
            "mom_pct": round(100.0 * (latest - prev) / prev, 1) if latest and prev else None,
            "target": targets or None,
            "achievement": round(100.0 * latest / targets, 1) if latest and targets else None,
            "best": next(iter(sorted((r for r in mine if r["latest"] is not None), key=lambda r: -r["latest"])), None)})
    moving = sorted((r for r in rows if r["mom_pct"] is not None), key=lambda r: r["mom_pct"])
    return {"months": shown, "current": current, "latest_month": latest_month, "prev_month": prev_month,
            "rows": rows, "sections": sections, "rank_months": rank_months, "group_totals": group_totals, "summary": summary, "has_data": bool(rows),
            "groups": list(CenterGroup.objects.values_list("name", flat=True)),
            "total_latest": sum(r["latest"] or 0 for r in rows),
            "total_average": sum(r["average"] or 0 for r in rows),
            "top_movers": moving[::-1][:5], "bottom_movers": moving[:5]}


def group_month_table(group="", today=None):
    """Each group by month for the last 12 months: gyms, the monthly total, average of the full months, and a total row."""
    data = matrix(group, 12, today)
    months, complete = data["months"], [m for m in data["months"] if m < data["current"]]
    rows = []
    for name, series in data["group_totals"].items():
        done = [series[m] for m in complete if series[m]]
        rows.append({"group": name, "gyms": sum(1 for r in data["rows"] if name in r["groups"]),
                     "cells": [series[m] or None for m in months],
                     "average": sum(done) / len(done) if done else None})
    order = {g: i for i, g in enumerate(list(CenterGroup.objects.values_list("name", flat=True)) + [UNGROUPED])}
    rows.sort(key=lambda r: order.get(r["group"], 99))
    total = [sum(r["cells"][i] or 0 for r in rows) or None for i in range(len(months))]  # a gym in two groups counts twice
    return {"months": months, "labels": [label(m) for m in months], "rows": rows, "total": total,
            "current": data["current"]}


def group_month_download(table_):
    headers = ["group", "gyms"] + table_["labels"] + ["average"]
    rows = [[r["group"], r["gyms"]] + r["cells"] + [None if r["average"] is None else round(r["average"])]
            for r in table_["rows"]]
    return headers, rows


def table(data):
    """Headers and rows for a download: the sheet's columns, one block per group, ranks within the group."""
    months = data["months"]
    headers = (["group", "center_id", "gym"] + [label(m) for m in months] +
               ["average"] + [f"ranking {label(m)}" for m in data["rank_months"]] +
               ["ranking", "change_vs_prev_month_%", "monthly_target", "target_achieved_%", "note"])
    rows = [[sec["group"], r["id"], r["name"]] + [r["values"].get(m) for m in months] +
            [None if r["average"] is None else round(r["average"])] + r["ranks"] +
            [r["rank_average"], r["mom_pct"], r["target"], r["achievement"], r["note"]]
            for sec in data["sections"] for r in sec["rows"]]
    return headers, rows
