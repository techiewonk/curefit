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


def matrix(group="", months=12, today=None):
    today = today or timezone.localdate()
    current = month_of(today)
    shown = _months(current, months)
    complete = [m for m in shown if m < current]
    latest_month = complete[-1] if complete else None
    prev_month = complete[-2] if len(complete) > 1 else None

    members = {}  # center id -> names of the groups it is in
    for gname, cid in CenterGroup.centers.through.objects.order_by("centergroup__position", "centergroup__name") \
            .values_list("centergroup__name", "center_id"):
        members.setdefault(cid, []).append(gname)

    names = PerfMetric.objects.order_by().values_list("metric", flat=True).distinct()
    total_metric = revenue_metrics(list(names))[0]
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
            "id": cid, "name": c.name, "groups": members.get(cid) or [UNGROUPED],
            "group": ", ".join(members.get(cid) or [UNGROUPED]), "values": values,
            "average": sum(done) / len(done) if done else None, "latest": latest,
            "mom_pct": round(100.0 * (latest - prev) / prev, 1) if latest is not None and prev else None,
            "target": c.monthly_target,
            "achievement": round(100.0 * latest / c.monthly_target, 1) if latest is not None and c.monthly_target else None})
    rank_latest = _ranks({r["id"]: r["latest"] for r in rows})
    rank_avg = _ranks({r["id"]: r["average"] for r in rows})
    for r in rows:
        r["rank_latest"], r["rank_average"] = rank_latest.get(r["id"]), rank_avg.get(r["id"])
    rows.sort(key=lambda r: (r["rank_average"] is None, r["rank_average"] or 0, r["name"]))

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
            "rows": rows, "group_totals": group_totals, "summary": summary, "has_data": bool(rows),
            "groups": list(CenterGroup.objects.values_list("name", flat=True)),
            "total_latest": sum(r["latest"] or 0 for r in rows),
            "total_average": sum(r["average"] or 0 for r in rows),
            "top_movers": moving[::-1][:5], "bottom_movers": moving[:5]}


def table(data):
    """Headers and rows for a download, laid out like the management sheet."""
    months = data["months"]
    headers = (["center_id", "gym", "group"] + [label(m) for m in months] +
               ["average", f"rank {label(data['latest_month'])}" if data["latest_month"] else "rank latest",
                "rank average", "change_vs_prev_month_%", "monthly_target", "target_achieved_%"])
    rows = [[r["id"], r["name"], r["group"]] + [r["values"].get(m) for m in months] +
            [None if r["average"] is None else round(r["average"]), r["rank_latest"], r["rank_average"],
             r["mom_pct"], r["target"], r["achievement"]] for r in data["rows"]]
    return headers, rows
