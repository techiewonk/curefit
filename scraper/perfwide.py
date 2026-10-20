"""Performance metrics as a wide table: one row per center and month, one column per metric.

The perf card stores one row per metric and date. Here those become columns, and several sessions that
cover the same month collapse to one figure: each month uses one split (day, week or month), the one covering
most of the month, then the newest; day and week rows are added up (rates averaged).
"""
import datetime as dt
import re
from collections import namedtuple

from django.db.models import Sum
from django.utils import timezone

from centers.models import Center, CenterGroup

from . import numbers
from .analysis import revenue_metrics
from .models import PerfMetric, Session
from .months import days_in, label, month_of, next_month  # noqa: F401 - label is used by the pages


def metric_names():
    return list(PerfMetric.objects.order_by().values_list("metric", flat=True).distinct().order_by("metric"))


def ordered_metrics(names):
    """Total / new / repeat revenue first, the rest alphabetically."""
    first = [m for m in revenue_metrics(names) if m]
    return first + [m for m in sorted(names) if m not in first]


class Figure(namedtuple("Figure", "value covered days estimated")):
    """One center's figure for a metric and month: the value, how many of the month's `days` it covers, and
    whether part of it was estimated (a week crossing the month end, split by days)."""

    @property
    def partial(self):
        return self.covered < self.days

    @property
    def coverage_text(self):
        return f"{self.covered} of {self.days} days"


MIXED_SPLIT_HELP = ("Each month uses one split (day, week or month): the one covering most days, then the newest. "
                    "A week that crosses a month end is split between the two months by days (marked estimated).")


def _span(day, gran, began, ended, created):
    """(first, last) day a perf row stands for: its day, week or month, cut to the session's period and to the
    day the session was fetched. A row lying outside its session's period (old data) keeps its own dates, up to
    the fetch day (at least its first day)."""
    if gran == Session.MONTH:
        first, last = month_of(day), next_month(month_of(day)) - dt.timedelta(days=1)
    elif gran == Session.WEEK:
        first, last = day, day + dt.timedelta(days=6)
    else:
        first = last = day
    fetched_on = timezone.localtime(created).date()
    lo, hi = max(first, began), min(last, ended, fetched_on)
    return (lo, hi) if lo <= hi else (first, min(last, max(first, fetched_on)))


def month_figures(since=None, until=None, center_ids=None, metrics=None):
    """{(center_id, month): {metric: Figure}} for the months from `since` to `until` (first-of-month dates).

    One figure per center, metric and month, chosen like this:
      * the rows are added up per session and date in SQL;
      * for each split (day, week, month) and date, the session covering more days wins, then the newest one,
        so a re-run corrects an earlier figure; coverage counts only up to the day the session was fetched;
      * day and week rows of a month are combined (added up, or a day-weighted average for rates like
        retention); a week crossing the month end is split between the two months by days (`estimated`);
      * the month then takes ONE split, never a mix: the one covering most days of it, then the newest.
    `covered` counts the days of the month those sessions asked for (their period, up to the day they were fetched),
    so a day without sales (no row) is not a gap; fewer than the month's days = a partly fetched month.
    """
    qs = PerfMetric.objects.filter(metric_date__isnull=False, value__isnull=False)
    if since:
        qs = qs.filter(metric_date__gte=since - dt.timedelta(days=6))  # a week starting in the month before
    if until:
        qs = qs.filter(metric_date__lt=next_month(until))
    if center_ids is not None:
        qs = qs.filter(center_id__in=list(center_ids))
    if metrics:
        qs = qs.filter(metric__in=list(metrics))
    rows = list(qs.order_by().values_list("center_id", "metric", "metric_date", "session_id").annotate(v=Sum("value")))
    sessions = {pk: rest for pk, *rest in Session.objects.filter(pk__in={r[3] for r in rows}).values_list(
        "pk", "granularity", "start_date", "end_date", "created_at")} if rows else {}
    chosen = {}  # (center, metric, granularity, date) -> (days covered, created, value, first, last)
    asked = {}  # (center, metric, month, granularity) -> sessions with rows there (their periods count as covered)
    for cid, metric, day, sid, value in rows:
        if not numbers.is_finite(value) or sid not in sessions:
            continue
        gran, began, ended, created = sessions[sid]
        first, last = _span(day, gran, began, ended, created)
        candidate = ((last - first).days + 1, created, value, first, last)
        key = (cid, metric, gran, day)
        if key not in chosen or candidate[:2] > chosen[key][:2]:
            chosen[key] = candidate
        month = month_of(first)
        while month <= last:
            asked.setdefault((cid, metric, month, gran), set()).add(sid)
            month = next_month(month)
    rates = {m: is_rate(m) for m in {k[1] for k in chosen}}  # once per name: the Settings lists are read here
    parts = {}  # (center, metric, month, granularity) -> [days set, newest, weighted sum, weight, estimated]
    for (cid, metric, gran, _), (length, created, value, first, last) in chosen.items():
        month = month_of(first)
        while month <= last:
            lo, hi = max(first, month), min(last, next_month(month) - dt.timedelta(days=1))
            share = (hi - lo).days + 1
            part = parts.setdefault((cid, metric, month, gran), [set(), created, 0.0, 0, False])
            part[0].update(lo + dt.timedelta(days=i) for i in range(share))
            part[1] = max(part[1], created)
            if rates[metric]:
                part[2], part[3] = part[2] + value * share, part[3] + share
            else:
                part[2] += value * share / length
            part[4] = part[4] or share < length
            month = next_month(month)
    # A day with no sales has no row, but the session did ask for it: the days of a session's period (up to the day
    # it was fetched) count as covered in every month where that session gave this center and metric a figure.
    for (cid, metric, month, gran), part in parts.items():
        month_end = next_month(month) - dt.timedelta(days=1)
        for sid in asked.get((cid, metric, month, gran), ()):
            _, began, ended, created = sessions[sid]
            lo, hi = max(began, month), min(ended, month_end, timezone.localtime(created).date())
            part[0].update(lo + dt.timedelta(days=i) for i in range((hi - lo).days + 1))
    best = {}  # (center, metric, month) -> (days covered, newest, Figure)
    for (cid, metric, month, _), (days, newest, total, weight, estimated) in parts.items():
        if (since and month < since) or (until and month > until):
            continue
        value = total / weight if weight else total
        candidate = (len(days), newest, Figure(value, len(days), days_in(month), estimated))
        key = (cid, metric, month)
        if key not in best or candidate[:2] > best[key][:2]:
            best[key] = candidate
    out = {}
    for (cid, metric, month), (_, _, figure) in best.items():
        out.setdefault((cid, month), {})[metric] = figure
    return out


def month_values(since=None, until=None, center_ids=None, metrics=None):
    """{(center_id, month): {metric: value}}: the values of `month_figures`, without the coverage."""
    return {key: {m: f.value for m, f in vals.items()}
            for key, vals in month_figures(since, until, center_ids, metrics).items()}


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


def extra_metric_names():
    """Metric names that are not one of the three revenue columns of CenterMonth."""
    names = metric_names()
    trio = {m for m in revenue_metrics(names) if m}
    return [m for m in ordered_metrics(names) if m not in trio]


def rebuild_months(session=None):
    """(Re)build the CenterMonth rows for a session's centers and months, or for everything.

    Returns the number of rows written. Safe to repeat: it always recomputes from the perf rows.
    """
    from django.utils import timezone

    from .models import CenterMonth
    since = until = ids = None
    if session is not None:
        session = Session.objects.get(pk=session.pk)
        since, until, ids = month_of(session.start_date), month_of(session.end_date), session.center_ids
    figures = month_figures(since, until, ids)
    if not figures:
        if session is None:  # no perf rows at all any more: nothing of the old table is still true
            CenterMonth.objects.all().delete()
        return 0
    values = {key: {m: f.value for m, f in vals.items()} for key, vals in figures.items()}
    trio = revenue_metrics(sorted({m for v in values.values() for m in v}))
    have = {}
    for chunk in range(0, len(ids or []) or 1, 900):
        qs = CenterMonth.objects.all()
        if ids is not None:
            qs = qs.filter(center_id__in=ids[chunk:chunk + 900])
        have.update({(c.center_id, c.month): c for c in qs})
        if ids is None:
            break
    now, new, changed = timezone.now(), [], []
    for (cid, month), vals in values.items():
        total = figures[(cid, month)].get(trio[0]) if trio[0] else None
        fields = dict(total_revenue=vals.get(trio[0]) if trio[0] else None,
                      new_revenue=vals.get(trio[1]) if trio[1] else None,
                      repeat_revenue=vals.get(trio[2]) if trio[2] else None,
                      covered_days=total.covered if total else None, estimated=bool(total and total.estimated),
                      metrics={k: round(v, 4) for k, v in vals.items()}, last_session=session, updated_at=now)
        row = have.get((cid, month))
        if row is None:
            new.append(CenterMonth(center_id=cid, month=month, **fields))
        else:
            for k, v in fields.items():
                setattr(row, k, v)
            changed.append(row)
    if session is None:  # a full rebuild also drops months whose sessions were deleted
        keep = set(values)
        CenterMonth.objects.filter(pk__in=[c.pk for c in CenterMonth.objects.all() if (c.center_id, c.month) not in keep]).delete()
    CenterMonth.objects.bulk_create(new, batch_size=500, ignore_conflicts=True)
    CenterMonth.objects.bulk_update(
        changed, ["total_revenue", "new_revenue", "repeat_revenue", "covered_days", "estimated", "metrics", "last_session",
                  "updated_at"], batch_size=500)
    return len(new) + len(changed)


# ------------------------------------------------------------------ analysis
def pct(new, old):
    """Percent change from old to new; None without a base. A negative base uses its size, so the sign is the direction."""
    return round(100.0 * (new - old) / abs(old), 1) if new is not None and old else None


_pct = pct


def parse_month(text, default=None):
    """'2026-08' (a month input) or '2026-08-01' -> first of that month; default when blank or wrong."""
    try:
        parts = str(text).split("-")
        month = dt.date(int(parts[0]), int(parts[1]), 1)
    except (ValueError, IndexError):
        return default
    limit = dt.date.today() + dt.timedelta(days=400)
    return month if dt.date(2000, 1, 1) <= month <= limit else default  # 0001-01 or 9999-12 are typos, not months


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
        while mm <= r[1] and len(out) < 120:  # ten years is plenty; a typo must not loop for ever
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
        cells = [val(cid, mm) for mm in months]
        full = [v for mm, v in zip(months, cells) if mm < current and v is not None]
        last_full, before_last = (cells[-2], cells[-3]) if len(cells) >= 3 else (None, None)
        rows.append({"id": cid, "name": c.name, "group": ", ".join(members.get(cid) or ["Ungrouped"]),
                     "groups": members.get(cid) or ["Ungrouped"], "now": now, "before": before,
                     "delta": None if now is None or before is None else now - before, "pct": _pct(now, before),
                     "cells": cells, "average": sum(full) / len(full) if full else None,
                     "mom_pct": _pct(last_full, before_last)})
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
    def total(values):
        return None if not values else (sum(values) if agg == "sum" else sum(values) / len(values))

    if mode == "group":
        total_now, total_before = total([r["now"] for r in rows if r["now"] is not None]), None
    else:  # the change is between the same gyms: one that has only one of the two figures would skew it
        both = [r for r in rows if r["now"] is not None and r["before"] is not None]
        total_now, total_before = total([r["now"] for r in both]), total([r["before"] for r in both])
    totals = []
    for i in range(len(months)):
        got = [r["cells"][i] for r in rows if r["cells"][i] is not None]
        totals.append(None if not got else (sum(got) if agg == "sum" else sum(got) / len(got)))
    return {"months": months, "lines": lines, "rows": rows, "month": month, "mode": mode, "agg": agg, "totals": totals,
            "total_now": total_now, "total_before": total_before, "total_pct": _pct(total_now, total_before)}


AVERAGE = "Average per gym"
_RATE = re.compile(r"%|per (member|user|gym|visit)|\b(rates?|ratings?|ratios?|retention|conversion|average|avg|churn|nps|"
                   r"occupancy|arpu)\b", re.I)


RATE_HELP = ("Rates (%, averages, per member, retention...) are averaged across gyms and days; counts and money are "
             "added up. Settings > Rate metrics / Not rate metrics override the guess for a metric name.")


def _named(setting):
    """Metric names listed in a Settings text (one per line or comma separated), lower case."""
    from constance import config
    try:
        text = getattr(config, setting)
    except AttributeError:  # the setting is not declared (yet)
        return set()
    return {n.strip().lower() for n in re.split(r"[\n,;]", str(text or "")) if n.strip()}


def is_rate(name):
    """Rates and ratings are averaged across gyms, counts and money are added up.

    The Settings lists RATE_METRICS / NOT_RATE decide first; otherwise the name is guessed from whole words:
    'Corporate' and 'Registrations' are not rates."""
    low = (name or "").strip().lower()
    if low in _named("NOT_RATE"):
        return False
    if low in _named("RATE_METRICS"):
        return True
    return bool(_RATE.search(name or ""))


def available_months():
    """The months that have data, oldest first."""
    from .models import CenterMonth
    return list(CenterMonth.objects.order_by().values_list("month", flat=True).distinct().order_by("month"))


def all_series(group="", agg="auto", months_back=12, today=None):
    """One chart per metric: {metric, lines: {group: [value per month]}}, over the last `months_back` months.

    agg: auto (rates averaged, the rest added up), sum, or avg.
    """
    from django.utils import timezone
    today = today or timezone.localdate()
    current = month_of(today)
    first = current
    for _ in range(months_back - 1):
        first = month_of(first - dt.timedelta(days=1))
    months, m = [], first
    while m <= current:
        months.append(m)
        m = next_month(m)
    values = month_values(first, None, center_ids_for(group))
    members = group_members()
    names = ordered_metrics(sorted({k for v in values.values() for k in v}))
    series = []
    for metric in names:
        how = ("avg" if is_rate(metric) else "sum") if agg == "auto" else agg
        buckets = {}
        for (cid, month), vals in values.items():
            if metric in vals:
                for g in members.get(cid) or ["Ungrouped"]:
                    buckets.setdefault(g, {}).setdefault(month, []).append(vals[metric])
        lines = {}
        for g, per_month in buckets.items():
            lines[g] = [None if not per_month.get(mm) else
                        (sum(per_month[mm]) if how == "sum" else sum(per_month[mm]) / len(per_month[mm]))
                        for mm in months]
        every = {}
        for (cid, month), vals in values.items():
            if metric in vals:
                every.setdefault(month, []).append(vals[metric])
        if lines:  # the mean over all gyms in the selection, so a group line can be read against it
            lines[AVERAGE] = [None if not every.get(mm) else sum(every[mm]) / len(every[mm]) for mm in months]
        series.append({"metric": metric, "how": how, "lines": lines})
    return months, series


def gym_series(center_id, months_back=6, today=None):
    """Every metric of one gym over the last months, next to the average of the gyms in its first group.

    Returns (months, rows): rows = [{metric, values, group_average, average, change_pct}], one per metric.
    """
    from django.utils import timezone
    today = today or timezone.localdate()
    current = month_of(today)
    first = current
    for _ in range(months_back - 1):
        first = month_of(first - dt.timedelta(days=1))
    months, m = [], first
    while m <= current:
        months.append(m)
        m = next_month(m)
    mine = month_values(first, None, [center_id])
    groups = group_members().get(center_id) or []
    peer_ids = [cid for cid, names in group_members().items() if groups and groups[0] in names]
    peers = month_values(first, None, peer_ids) if peer_ids else {}
    names = ordered_metrics(sorted({k for v in mine.values() for k in v}))
    rows = []
    for metric in names:
        values = [mine.get((center_id, mm), {}).get(metric) for mm in months]
        group_avg = []
        for mm in months:
            got = [peers[(cid, mm)][metric] for cid in peer_ids if (cid, mm) in peers and metric in peers[(cid, mm)]]
            group_avg.append(sum(got) / len(got) if got else None)
        full = [v for mm, v in zip(months, values) if mm < current and v is not None]
        last_full, before_last = (values[-2], values[-3]) if len(values) >= 3 else (None, None)
        rows.append({"metric": metric, "values": values, "group_average": group_avg,
                     "average": sum(full) / len(full) if full else None, "change_pct": _pct(last_full, before_last)})
    return months, rows, (groups[0] if groups else "")
