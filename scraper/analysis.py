"""Per-center revenue analysis for a session (ORM version of the old build_analysis)."""
import re

from django.db.models import Count, Q, Sum

from centers.models import Center

from .models import PerfMetric, SalesRecord

HEADER = ["center_id", "center_name", "sales_count", "sales_revenue", "renewals", "renewal_rate_%",
          "total_revenue", "new_revenue", "repeat_revenue", "new_%", "repeat_%"]


def find_metric(metrics, *needles):
    for m in metrics:
        low = m.lower()
        if all(n in low for n in needles):
            return m
    return None


def build_analysis(session):
    """{'header': [...], 'rows': [[...]]} — one row per center that has sales or perf data."""
    metrics = list(PerfMetric.objects.filter(session=session).order_by("metric")
                   .values_list("metric", flat=True).distinct())
    m_total, m_new, m_repeat = revenue_metrics(metrics)

    sales = {r["center_id"]: r for r in SalesRecord.objects.filter(session=session)
             .values("center_id").annotate(n=Count("id"), rev=Sum("revenue"),
                                           ren=Count("id", filter=Q(renewed="Yes")))}

    def perf(metric):
        if not metric:
            return {}
        return {r["center_id"]: r["v"] for r in PerfMetric.objects.filter(session=session, metric=metric)
                .values("center_id").annotate(v=Sum("value"))}

    totals, news, repeats = perf(m_total), perf(m_new), perf(m_repeat)
    ids = sorted(set(sales) | set(PerfMetric.objects.filter(session=session)
                                  .values_list("center_id", flat=True).distinct()))
    names = dict(Center.objects.filter(center_id__in=ids).values_list("center_id", "name"))

    rows = []
    for cid in ids:
        s = sales.get(cid, {})
        count, rev, ren = s.get("n", 0), s.get("rev") or 0, s.get("ren", 0)
        total, new, rep = totals.get(cid), news.get(cid), repeats.get(cid)
        rows.append([
            cid, names.get(cid, ""), count, round(rev, 0), ren,
            round(100.0 * ren / count, 1) if count else 0.0,
            round(total, 0) if total else None,
            round(new, 0) if new is not None else None,
            round(rep, 0) if rep is not None else None,
            round(100.0 * new / total, 1) if total and new is not None else None,
            round(100.0 * rep / total, 1) if total and rep is not None else None,
        ])
    return {"header": HEADER, "rows": rows}


def revenue_metrics(metrics):
    """Names of the total / new / repeat revenue metrics among the perf metric names.

    'Total Revenue' must not be mistaken for 'Total Online Revenue', 'Net Total Revenue' or 'Last Month Total
    Revenue': the name is matched as a whole, with only a numbering in front ('1.', '1.a', '1.a.'); a numbered
    name is preferred. The old substring match is only a fallback when no name matches that way.
    """
    names = sorted(metrics)
    numbering = r"^(?:(\d+(?:\.[a-z0-9]*)*\.?)\s*)?"

    def exact(words):
        pattern = re.compile(numbering + words + r"$", re.I)
        found = [(m, pattern.match(m.strip())) for m in names]
        found = [(m, hit) for m, hit in found if hit]
        return next((m for m, hit in found if hit.group(1)), found[0][0] if found else None)

    def loose(first, *rest):
        """Fallback: a name that STARTS with the word (after a numbering), e.g. 'Total Revenue (INR)'; only then any
        name holding the words, so 'Net Total Revenue' or 'Renewal Revenue' are a last resort."""
        start = re.compile(numbering + re.escape(first) + r"\b", re.I)
        return next((m for m in names if start.match(m.strip()) and all(w in m.lower() for w in rest)), None) \
            or find_metric(names, first, *rest)

    return (exact("total revenue") or loose("total", "revenue") or find_metric(names, "1.", "total"),
            exact("new revenue") or loose("new", "revenue"),
            exact("repeat revenue") or loose("repeat", "revenue"))


def trend(session):
    """Revenue over time: one point per day / week / month the session was split by, all centers added."""
    names = list(PerfMetric.objects.filter(session=session).order_by("metric")
                 .values_list("metric", flat=True).distinct())
    series = {}
    for key, metric in zip(("total", "new", "repeat"), revenue_metrics(names)):
        if metric:
            for row in PerfMetric.objects.filter(session=session, metric=metric, metric_date__isnull=False) \
                    .values("metric_date").annotate(v=Sum("value")):
                series.setdefault(row["metric_date"], {})[key] = row["v"]
    return [{"date": day, **series[day]} for day in sorted(series)]
