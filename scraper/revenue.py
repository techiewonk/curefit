"""Revenue by group and month: the management view (gyms x months, average, ranking, growth, target).

Source: the 'Total Revenue' metric of the Franchise Performance card, split by month. Several sessions can
cover the same month; for each center and month the newest session's numbers are used, so a re-run
corrects the figure instead of adding to it.
"""
import datetime as dt

from django.utils import timezone

from centers.models import Center, CenterGroup

from . import perfwide
from .analysis import revenue_metrics
from .models import CenterMonth, PerfMetric
from .months import days_in, label, month_of, next_month  # noqa: F401 - label / next_month are re-exported

UNGROUPED = "Ungrouped"


def _months(current, count):
    out, m = [], current
    for _ in range(count):
        out.append(m)
        m = month_of(m - dt.timedelta(days=1))
    return out[::-1]


def _ranks(values):
    """{key: rank}, highest first; keys with no value are not ranked. Equal values share a rank (1, 2, 2, 4)."""
    present = {k: v for k, v in values.items() if v is not None}
    return {k: 1 + sum(1 for other in present.values() if other > v) for k, v in present.items()}


ALL_MONTHS, MOST_MONTHS = 0, 48


def group_order():
    return list(CenterGroup.objects.values_list("name", flat=True)) + [UNGROUPED]


PARTIAL_HELP = ("A month marked 'N of M days' was only partly fetched: it is shown, but kept out of the average, "
                "the rankings and the change vs the previous month. Run that month again to complete it.")


def _first_month(total_metric):
    first = PerfMetric.objects.filter(metric=total_metric, metric_date__isnull=False).order_by("metric_date") \
        .values_list("metric_date", flat=True).first() if total_metric else None
    return month_of(first) if first else None


def _stored(since, ids):
    """{(center, month): Figure} of the total revenue from the stored monthly table (CenterMonth): no raw rows read."""
    qs = CenterMonth.objects.filter(month__gte=since, total_revenue__isnull=False)
    if ids is not None:
        qs = qs.filter(center_id__in=ids)
    out = {}
    for cid, month, value, covered, estimated in qs.values_list(
            "center_id", "month", "total_revenue", "covered_days", "estimated"):
        out[(cid, month)] = perfwide.Figure(value, days_in(month) if covered is None else covered, days_in(month),
                                            estimated)
    return out


def _empty(current, notice, group_names):
    return {"months": [], "current": current, "latest_month": None, "prev_month": None, "rows": [], "sections": [],
            "rank_months": [], "group_totals": {}, "group_partial": {}, "summary": [], "has_data": False,
            "group_order": group_order(), "groups": group_names, "total_latest": 0, "total_average": 0,
            "top_movers": [], "bottom_movers": [], "notice": notice, "partial_help": PARTIAL_HELP}


def matrix(group="", months=12, today=None, since=None, until=None, stored=False):
    """months: how many to show ending with the current one; 0 = from the first month that has data.

    since / until (first-of-month dates) narrow the months shown; a range with no month in the window gives an
    empty result with a `notice`. group limits everything to that group. stored=True reads the stored monthly
    table (CenterMonth) instead of the raw perf rows: cheap enough for the Home cards.

    A past month that was only partly fetched is shown, flagged in row["partial"] ({month: 'N of M days'}), and kept
    out of the average, the rankings and mom_pct."""
    today = today or timezone.localdate()
    current = month_of(today)
    names = sorted(set(PerfMetric.objects.order_by().values_list("metric", flat=True).distinct()))
    total_metric = revenue_metrics(names)[0]
    if months == ALL_MONTHS:
        first = _first_month(total_metric)
        months = 1 if first is None else max(1, min(MOST_MONTHS, (current.year - first.year) * 12 + current.month - first.month + 1))
    shown = _months(current, max(1, months))
    if since or until:
        narrowed = [m for m in shown if (not since or m >= since) and (not until or m <= until)]
        if not narrowed:
            first = _first_month(total_metric)
            starts = f"data starts {label(first)}" if first else "no revenue data yet"
            return _empty(current, f"No months in this range ({starts}; the table covers {label(shown[0])} to "
                                   f"{label(shown[-1])}).", list(CenterGroup.objects.values_list("name", flat=True)))
        shown = narrowed
    complete = [m for m in shown if m < current]
    latest_month = complete[-1] if complete else None
    prev_month = complete[-2] if len(complete) > 1 else None

    members = perfwide.group_members()  # center id -> names of the groups it is in
    best = {}  # (center, month) -> Figure
    if total_metric:
        ids = None
        if group and group != UNGROUPED:
            ids = [cid for cid, names_ in members.items() if group in names_]
        if stored and CenterMonth.objects.exists():
            found = _stored(shown[0], ids)
        else:
            found = {key: vals[total_metric] for key, vals in
                     perfwide.month_figures(shown[0], None, ids, [total_metric]).items() if total_metric in vals}
        for (cid, month), figure in found.items():
            if not (group == UNGROUPED and cid in members):
                best[(cid, month)] = figure

    centers = {c.pk: c for c in Center.objects.filter(pk__in={cid for cid, _ in best})}
    rows = []
    for cid, c in centers.items():
        values = {m: best[(cid, m)].value for m in shown if (cid, m) in best}
        partial = {m: best[(cid, m)].coverage_text for m in complete if (cid, m) in best and best[(cid, m)].partial}
        full = {m: v for m, v in values.items() if m in complete and m not in partial}  # what averages and ranks use
        done = [full[m] for m in complete if m in full]
        latest = values.get(latest_month)
        rows.append({
            "id": cid, "name": c.name, "note": c.note, "groups": members.get(cid) or [UNGROUPED],
            "group": ", ".join(members.get(cid) or [UNGROUPED]), "values": values, "partial": partial,
            "estimated": [m for m in shown if (cid, m) in best and best[(cid, m)].estimated], "full": full,
            "average": sum(done) / len(done) if done else None, "latest": latest,
            "mom_pct": perfwide.pct(full.get(latest_month), full.get(prev_month)),
            "target": c.monthly_target,
            "achievement": round(100.0 * latest / c.monthly_target, 1) if latest is not None and c.monthly_target else None})
    rows.sort(key=lambda r: r["name"])

    # one table per group, like the sheet; ranks are within the group (a gym in two groups ranks in each)
    order = [group] if group else group_order()
    sections = []
    rank_months = shown[-6:]  # a ranking column for each of the last six months (the current one is partial)
    for name in order:
        mine = [dict(r) for r in rows if name in r["groups"]]
        if not mine:
            continue
        rank_latest = _ranks({r["id"]: r["full"].get(latest_month) for r in mine})
        rank_avg = _ranks({r["id"]: r["average"] for r in mine})
        by_month = {m: _ranks({r["id"]: r["full"].get(m) if m < current else r["values"].get(m) for r in mine})
                    for m in rank_months}
        for r in mine:
            r["rank_latest"], r["rank_average"] = rank_latest.get(r["id"]), rank_avg.get(r["id"])
            r["ranks"] = [by_month[m].get(r["id"]) for m in rank_months]
        mine.sort(key=lambda r: (r["rank_latest"] is None, r["rank_latest"] or 0, r["name"]))
        sections.append({"group": name, "rows": mine})

    group_totals = {}  # a center in two groups counts in both; None = no gym had a figure that month
    group_partial = {}  # group -> months where one of its gyms was only partly fetched
    for r in rows:
        for g in r["groups"]:
            if group and g != group:
                continue
            series = group_totals.setdefault(g, {m: None for m in shown})
            for m, v in r["values"].items():
                series[m] = (series[m] or 0) + v
            group_partial.setdefault(g, set()).update(r["partial"])
    summary = []
    for name, series in sorted(group_totals.items(), key=lambda kv: -(kv[1].get(latest_month) or 0)):
        mine = [r for r in rows if name in r["groups"]]
        part = group_partial.get(name, set())
        done = [series[m] for m in complete if series[m] is not None and m not in part]
        latest = series.get(latest_month)
        prev = series.get(prev_month) if prev_month not in part else None
        targets = sum(r["target"] or 0 for r in mine)
        summary.append({
            "group": name, "gyms": len(mine), "latest": latest, "average": sum(done) / len(done) if done else None,
            "partial": latest_month in part,
            "mom_pct": perfwide.pct(latest if latest_month not in part else None, prev),
            "target": targets or None,
            "achievement": round(100.0 * latest / targets, 1) if latest is not None and targets else None,
            "best": next(iter(sorted((r for r in mine if r["latest"] is not None), key=lambda r: -r["latest"])), None)})
    moving = sorted((r for r in rows if r["mom_pct"] is not None), key=lambda r: r["mom_pct"])
    return {"months": shown, "current": current, "latest_month": latest_month, "prev_month": prev_month,
            "rows": rows, "sections": sections, "rank_months": rank_months, "group_totals": group_totals,
            "group_partial": group_partial,
            "summary": summary, "has_data": bool(rows), "group_order": group_order(),
            "groups": list(CenterGroup.objects.values_list("name", flat=True)),
            "total_latest": sum(r["latest"] or 0 for r in rows),
            "total_average": sum(r["average"] or 0 for r in rows),
            "top_movers": moving[::-1][:5], "bottom_movers": moving[:5],
            "notice": None, "partial_help": PARTIAL_HELP}


def group_month_table(group="", today=None, data=None):
    """Each group by month for the last 12 months: gyms, the monthly total, average of the full months, and a total row.

    data: a `matrix(group, 12, today)` the caller already has, so the page does not compute it twice."""
    data = data or matrix(group, 12, today)
    months, complete = data["months"], [m for m in data["months"] if m < data["current"]]
    rows = []
    for name, series in data["group_totals"].items():
        part = data.get("group_partial", {}).get(name, set())
        done = [series[m] for m in complete if series[m] is not None and m not in part]
        rows.append({"group": name, "gyms": sum(1 for r in data["rows"] if name in r["groups"]),
                     "cells": [series[m] for m in months],
                     "average": sum(done) / len(done) if done else None})
    order = {g: i for i, g in enumerate(list(CenterGroup.objects.values_list("name", flat=True)) + [UNGROUPED])}
    rows.sort(key=lambda r: order.get(r["group"], 99))
    total = [None if all(r["cells"][i] is None for r in rows) else sum(r["cells"][i] or 0 for r in rows)
             for i in range(len(months))]  # a gym in two groups counts twice
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
