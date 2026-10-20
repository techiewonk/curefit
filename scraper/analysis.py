"""Per-center revenue analysis for a session (ORM version of the old build_analysis)."""
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
    """Names of the total / new / repeat revenue metrics among the perf metric names."""
    return (find_metric(metrics, "total", "revenue") or find_metric(metrics, "1.", "total"),
            find_metric(metrics, "new", "revenue"), find_metric(metrics, "repeat", "revenue"))


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
