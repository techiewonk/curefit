"""Files on disk for a session: per-center CSVs, merged/ (one file per report), period reports."""
import json
import os

import tablib
from django.conf import settings
from django.contrib.humanize.templatetags.humanize import intcomma
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.text import slugify

from centers.models import Center

from . import analysis
from .models import CardColumns, CardRow, GymMember, PerfMetric, SalesRecord
from .reports import GENERIC, REPORTS, TYPED_COLUMNS
from .resources import GymMemberResource, PerfMetricResource, SalesRecordResource

RUNS_DIR = os.environ.get("CUREFIT_RUNS_DIR") or os.path.join(settings.BASE_DIR, "runs")


def local(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%dT%H:%M:%S") if dt else ""


def save_csv(path, dataset):
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(dataset.export("csv"))
    return path


def session_folder(start, end, session_id):
    return os.path.join(RUNS_DIR, timezone.localdate().isoformat(), f"{start}_to_{end}_{session_id}")


def write_session_json(session):
    """Mirror of the Session row next to its files (handy when browsing the folder)."""
    if not session.folder:
        return
    os.makedirs(session.folder, exist_ok=True)
    with open(os.path.join(session.folder, "session.json"), "w", encoding="utf-8") as f:
        json.dump({"session_id": session.session_id, "start": str(session.start_date),
                   "end": str(session.end_date), "status": session.status,
                   "created_at": local(session.created_at), "finished_at": local(session.finished_at),
                   "centers": session.center_ids, "reports": session.reports,
                   "missing": session.missing}, f, indent=2)


def write_center_file(session, center, report, cols, rows):
    """centers/<id>_<name>/<report>.csv inside the session folder."""
    width = len(rows[0]) if rows else 0
    typed = TYPED_COLUMNS.get(report, [])
    header = cols if cols and len(cols) == width else \
        (typed if len(typed) == width else None) or [f"col{i + 1}" for i in range(width)]
    folder = os.path.join(session.folder, "centers", f"{center.center_id}_{slugify(center.name or center.code)}")
    os.makedirs(folder, exist_ok=True)
    save_csv(os.path.join(folder, f"{report}.csv"), tablib.Dataset(*rows, headers=header))


# ------------------------------------------------------------------ merged/
def merge_session(session):
    """One file per report holding every center's rows, plus all_reports.xlsx (one sheet each)."""
    centers_dir = os.path.join(session.folder, "centers")
    if not os.path.isdir(centers_dir):
        return []
    out_dir = os.path.join(session.folder, "merged")
    os.makedirs(out_dir, exist_ok=True)
    names = dict(Center.objects.values_list("center_id", "name"))

    parts = {}  # report -> [(center id, csv path)]
    for folder in sorted(os.listdir(centers_dir), key=lambda d: int(d.split("_")[0])):
        for name in sorted(os.listdir(os.path.join(centers_dir, folder))):
            if name.endswith(".csv"):
                parts.setdefault(name[:-4], []).append(
                    (int(folder.split("_")[0]), os.path.join(centers_dir, folder, name)))

    written, sheets = [], []
    for report in [r for r in REPORTS if r in parts]:
        loaded = []
        for cid, path in parts[report]:
            with open(path, newline="", encoding="utf-8") as f:
                loaded.append((cid, tablib.Dataset().load(f.read(), format="csv")))
        header = []  # centers can differ in columns: keep the union, in the order seen
        for _, ds in loaded:
            header += [h for h in ds.headers if h not in header]
        rows = [[cid, names.get(cid, "")] + [row.get(h, "") for h in header] for cid, ds in loaded for row in ds.dict]
        merged = tablib.Dataset(*rows, headers=["center_id", "center_name"] + header, title=report[:31])
        written.append(save_csv(os.path.join(out_dir, f"{report}.csv"), merged))
        sheets.append(merged)

    xlsx = os.path.join(out_dir, "all_reports.xlsx")
    with open(xlsx, "wb") as f:
        f.write(tablib.Databook(sheets).export("xlsx"))
    return written + [xlsx]


# ------------------------------------------------------------ period reports
def export_reports(session):
    """sales_report / perf_long / perf_pivot / gym_members / card CSVs, analysis.csv, dashboard.html."""
    written = []

    def csv_file(name, dataset):
        written.append(save_csv(os.path.join(session.folder, name), dataset))

    for name, model, resource in (("sales_report", SalesRecord, SalesRecordResource),
                                  ("perf_long", PerfMetric, PerfMetricResource),
                                  ("gym_members", GymMember, GymMemberResource)):
        queryset = model.objects.filter(session=session).select_related("center", "session").order_by("center_id")
        csv_file(f"{name}.csv", resource().export(queryset))

    by_center, metrics = {}, set()
    for cid, name, metric, value in PerfMetric.objects.filter(session=session).order_by("center_id") \
            .values_list("center_id", "center__name", "metric", "value"):
        by_center.setdefault((cid, name), {})[metric] = value
        metrics.add(metric)
    metrics = sorted(metrics)
    csv_file("perf_pivot.csv", tablib.Dataset(
        *([cid, name] + [values.get(m) for m in metrics] for (cid, name), values in by_center.items()),
        headers=["center_id", "center_name"] + metrics))

    for report in GENERIC:
        columns = CardColumns.objects.filter(report=report).values_list("columns", flat=True).first()
        rows = list(CardRow.objects.filter(session=session, report=report).select_related("center")
                    .order_by("center_id", "row_num"))
        if columns and rows:
            csv_file(f"{report}.csv", tablib.Dataset(
                *([r.center_id, r.center.name, r.center.code] + (list(r.data) + [None] * len(columns))[:len(columns)]
                  + [local(r.scraped_at)] for r in rows),
                headers=["center_id", "center_name", "center_code"] + columns + ["scraped_at"]))

    result = analysis.build_analysis(session)
    csv_file("analysis.csv", tablib.Dataset(*result["rows"], headers=result["header"]))
    dashboard = os.path.join(session.folder, "dashboard.html")
    with open(dashboard, "w", encoding="utf-8") as f:
        f.write(_dashboard_html(session, result))
    return written + [dashboard], result


def _dashboard_html(session, result):
    """The standalone page (opens by double-click). Django's template escapes every value."""
    rows = result["rows"]
    total = lambda col: sum(r[col] or 0 for r in rows)
    return render_to_string("scraper/session_dashboard.html", {
        "start": session.start_date, "end": session.end_date, "header": result["header"], "rows": rows,
        "cards": [("Sales count", intcomma(total(2))), ("Sales revenue", f"₹{intcomma(round(total(3)))}"),
                  ("Total revenue", f"₹{intcomma(round(total(6)))}"), ("New revenue", f"₹{intcomma(round(total(7)))}"),
                  ("Repeat revenue", f"₹{intcomma(round(total(8)))}")],
        "chart": {"names": [r[1] or str(r[0]) for r in rows], "total": [r[6] for r in rows],
                  "new": [r[7] for r in rows], "repeat": [r[8] for r in rows]}})


def write_coverage(session, stats):
    """coverage.csv for the folder; returns the rows (also stored on the Session)."""
    rows = [{"report": report, "requested": st["requested"], "already_done": st["already_done"],
             "scraped": st["ok"], "empty": len(st["empty"]), "known_empty": len(st["known_empty"]),
             "failed": len(st["failed"]), "slow": len(st["slow"]), "possibly_truncated": len(st["capped"]),
             "skipped_after_failures": st["skipped"], "empty_center_ids": st["empty"],
             "failed_center_ids": st["failed"]} for report, st in stats.items()]
    headers = list(rows[0]) if rows else ["report"]
    save_csv(os.path.join(session.folder, "coverage.csv"), tablib.Dataset(
        *([" ".join(map(str, v)) if isinstance(v, list) else v for v in row.values()] for row in rows),
        headers=headers))
    return rows
