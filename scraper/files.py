"""Files on disk for a session: per-center CSVs, merged/ (one file per report), period reports."""
import csv
import json
import os
import re

from django.conf import settings
from django.utils import timezone

from centers.models import Center

from . import analysis
from .models import PerfMetric, SalesRecord, GymMember, CardColumns, CardRow, Session
from .reports import GENERIC, REPORTS, TYPED_COLUMNS

RUNS_DIR = os.environ.get("CUREFIT_RUNS_DIR") or os.path.join(settings.BASE_DIR, "runs")


def slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-") or "center"


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def local(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%dT%H:%M:%S") if dt else ""


def session_folder(start, end, session_id):
    day = timezone.localdate().isoformat()
    return os.path.join(RUNS_DIR, day, f"{start}_to_{end}_{session_id}")


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
    d = os.path.join(session.folder, "centers", f"{center.center_id}_{slug(center.name or center.code)}")
    os.makedirs(d, exist_ok=True)
    write_csv(os.path.join(d, f"{report}.csv"), header, rows)


# ------------------------------------------------------------------ merged/
def merge_session(session):
    """One file per report holding every center's rows (+ all_reports.xlsx when openpyxl exists)."""
    centers_dir = os.path.join(session.folder, "centers")
    out_dir = os.path.join(session.folder, "merged")
    if not os.path.isdir(centers_dir):
        return []
    os.makedirs(out_dir, exist_ok=True)
    names = dict(Center.objects.values_list("center_id", "name"))

    parts = {}
    for d in sorted(os.listdir(centers_dir), key=lambda x: int(x.split("_")[0]) if x.split("_")[0].isdigit() else 0):
        cid = d.split("_")[0]
        for f in sorted(os.listdir(os.path.join(centers_dir, d))):
            if f.endswith(".csv"):
                parts.setdefault(f[:-4], []).append((cid, os.path.join(centers_dir, d, f)))

    written, merged = [], {}
    for report in [r for r in REPORTS if r in parts]:
        header, rows = [], []
        for cid, path in parts[report]:
            with open(path, newline="", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                cols = next(reader, [])
                for c in cols:  # centers can differ in columns: keep the union, in the order seen
                    if c not in header:
                        header.append(c)
                for r in reader:
                    rows.append((cid, dict(zip(cols, r))))
        full = ["center_id", "center_name"] + header
        data = [[cid, names.get(int(cid), "")] + [row.get(c, "") for c in header] for cid, row in rows]
        path = os.path.join(out_dir, f"{report}.csv")
        write_csv(path, full, data)
        written.append(path)
        merged[report] = (full, data)

    try:
        from openpyxl import Workbook
        wb = Workbook(write_only=True)
        for report, (full, data) in merged.items():
            ws = wb.create_sheet(report[:31])
            ws.append(full)
            for r in data:
                ws.append(r)
        xlsx = os.path.join(out_dir, "all_reports.xlsx")
        wb.save(xlsx)
        written.append(xlsx)
    except ImportError:
        pass
    return written


# ------------------------------------------------------------ period reports
def export_reports(session):
    """sales_report / perf_long / perf_pivot / gym_members / card CSVs, analysis + dashboard.html."""
    out = lambda name: os.path.join(session.folder, name)
    written = []
    sd, ed = str(session.start_date), str(session.end_date)

    cols = ["center_id", "center_name", "center_code", "start_date", "end_date", "user_id", "name",
            "phone", "pack_name", "member_end_date", "active_last_30d", "renewed", "revenue",
            "scraped_at"]
    qs = SalesRecord.objects.filter(session=session).select_related("center").order_by("center_id", "name")
    write_csv(out("sales_report.csv"), cols, (
        [r.center_id, r.center.name, r.center.code, sd, ed, r.user_id, r.name, r.phone, r.pack_name,
         r.member_end_date, r.active_last_30d, r.renewed, r.revenue, local(r.scraped_at)]
        for r in qs.iterator(chunk_size=2000)))
    written.append(out("sales_report.csv"))

    pcols = ["center_id", "center_name", "center_code", "start_date", "end_date", "metric_date",
             "metric", "value", "scraped_at"]
    pq = PerfMetric.objects.filter(session=session).select_related("center").order_by("center_id", "metric")
    perf = list(pq)
    write_csv(out("perf_long.csv"), pcols, (
        [r.center_id, r.center.name, r.center.code, sd, ed, r.metric_date, r.metric, r.value,
         local(r.scraped_at)] for r in perf))
    written.append(out("perf_long.csv"))

    metrics = sorted({r.metric for r in perf})
    by_center = {}
    for r in perf:
        by_center.setdefault((r.center_id, r.center.name), {})[r.metric] = r.value
    write_csv(out("perf_pivot.csv"), ["center_id", "center_name"] + metrics,
              ([cid, name] + [vals.get(m) for m in metrics] for (cid, name), vals in sorted(by_center.items())))
    written.append(out("perf_pivot.csv"))

    gcols = ["center_id", "center_name", "center_code", "start_date", "end_date", "user_id", "username",
             "pack_name", "final_amount_paid", "purchase_date", "pack_start_date", "pack_end_date",
             "status", "offline_vs_online", "new_vs_repeat", "class_level", "last_class_date",
             "scraped_at"]
    gq = GymMember.objects.filter(session=session).select_related("center").order_by("center_id", "username")
    write_csv(out("gym_members.csv"), gcols, (
        [r.center_id, r.center.name, r.center.code, sd, ed, r.user_id, r.username, r.pack_name,
         r.final_amount_paid, r.purchase_date, r.pack_start_date, r.pack_end_date, r.status,
         r.offline_vs_online, r.new_vs_repeat, r.class_level, r.last_class_date, local(r.scraped_at)]
        for r in gq.iterator(chunk_size=2000)))
    written.append(out("gym_members.csv"))

    for report in GENERIC:
        columns = CardColumns.objects.filter(report=report).values_list("columns", flat=True).first()
        rows = list(CardRow.objects.filter(session=session, report=report).select_related("center")
                    .order_by("center_id", "row_num"))
        if columns is None or not rows:
            continue
        data = []
        for r in rows:
            vals = list(r.data) + [None] * (len(columns) - len(r.data))
            data.append([r.center_id, r.center.name, r.center.code] + vals + [local(r.scraped_at)])
        write_csv(out(f"{report}.csv"), ["center_id", "center_name", "center_code"] + columns + ["scraped_at"], data)
        written.append(out(f"{report}.csv"))

    result = analysis.build_analysis(session)
    write_csv(out("analysis.csv"), result["header"], result["rows"])
    written.append(out("analysis.csv"))
    write_html(out("dashboard.html"), sd, ed, result)
    written.append(out("dashboard.html"))
    return written, result


def write_coverage(session, stats):
    """coverage.csv for the folder; returns the rows (also stored on the Session)."""
    rows = [{"report": rep, "requested": st["requested"], "already_done": st["already_done"],
             "scraped": st["ok"], "empty": len(st["empty"]), "failed": len(st["failed"]),
             "slow": len(st["slow"]), "possibly_truncated": len(st["capped"]), "skipped_after_failures": st["skipped"],
             "empty_center_ids": st["empty"], "failed_center_ids": st["failed"]}
            for rep, st in stats.items()]
    write_csv(os.path.join(session.folder, "coverage.csv"),
              ["report", "requested", "already_done", "scraped", "empty", "failed", "slow",
               "possibly_truncated", "skipped_after_failures", "empty_center_ids", "failed_center_ids"],
              ([r["report"], r["requested"], r["already_done"], r["scraped"], r["empty"], r["failed"],
                r["slow"], r["possibly_truncated"], "yes" if r["skipped_after_failures"] else "",
                " ".join(map(str, r["empty_center_ids"])), " ".join(map(str, r["failed_center_ids"]))]
               for r in rows))
    return rows


_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Curefit Sales Dashboard __START__ to __END__</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
 body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
 header{padding:20px 28px;background:#161a22;border-bottom:1px solid #262b36}
 h1{margin:0;font-size:20px} .sub{color:#8b93a1;font-size:13px;margin-top:4px}
 .wrap{padding:24px 28px;max-width:1200px;margin:0 auto}
 .cards{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px}
 .card{background:#161a22;border:1px solid #262b36;border-radius:10px;padding:16px 20px;min-width:170px}
 .card .lbl{color:#8b93a1;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
 .card .val{font-size:24px;font-weight:600;margin-top:6px}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:24px}
 .panel{background:#161a22;border:1px solid #262b36;border-radius:10px;padding:18px}
 .panel h2{font-size:14px;margin:0 0 12px;color:#c7cdd8}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:8px 10px;text-align:right;border-bottom:1px solid #262b36}
 th:first-child,td:first-child{text-align:left}
 th{color:#8b93a1;font-weight:600;position:sticky;top:0;background:#161a22}
 tr:hover td{background:#1c212b}
 @media(max-width:900px){.grid{grid-template-columns:1fr}}
</style></head><body>
<header><h1>Curefit Sales Dashboard</h1><div class="sub">Period __START__ &rarr; __END__</div></header>
<div class="wrap">
 <div class="cards" id="cards"></div>
 <div class="grid">
   <div class="panel"><h2>Revenue by center</h2><canvas id="revChart" height="220"></canvas></div>
   <div class="panel"><h2>New vs Repeat revenue</h2><canvas id="mixChart" height="220"></canvas></div>
 </div>
 <div class="panel"><h2>Per-center detail</h2><div style="overflow:auto;max-height:520px"><table id="tbl"></table></div></div>
</div>
<script>
const DATA = __DATA__;
const TOTALS = __TOTALS__;
const esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = n => n==null?'-':'\\u20b9'+Number(n).toLocaleString('en-IN');
const num = n => n==null?'-':Number(n).toLocaleString('en-IN');
const cards=[['Sales count',num(TOTALS.sales_count)],['Sales revenue',fmt(TOTALS.sales_revenue)],
 ['Total revenue',fmt(TOTALS.total_revenue)],['New revenue',fmt(TOTALS.new_revenue)],['Repeat revenue',fmt(TOTALS.repeat_revenue)]];
document.getElementById('cards').innerHTML=cards.map(c=>`<div class="card"><div class="lbl">${esc(c[0])}</div><div class="val">${esc(c[1])}</div></div>`).join('');
const H=DATA.header, R=DATA.rows;
let t='<tr>'+H.map(h=>`<th>${esc(h)}</th>`).join('')+'</tr>';
t+=R.map(r=>'<tr>'+r.map((v,i)=>`<td>${v==null?'':esc([3,6,7,8].includes(i)?fmt(v):v)}</td>`).join('')+'</tr>').join('');
document.getElementById('tbl').innerHTML=t;
const names=R.map(r=>r[1]||r[0]);
const idx=n=>H.indexOf(n);
new Chart(document.getElementById('revChart'),{type:'bar',data:{labels:names,
 datasets:[{label:'Total revenue',data:R.map(r=>r[idx('total_revenue')]),backgroundColor:'#4f8cff'}]},
 options:{plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#8b93a1'}},y:{ticks:{color:'#8b93a1'}}}}});
new Chart(document.getElementById('mixChart'),{type:'bar',data:{labels:names,
 datasets:[{label:'New',data:R.map(r=>r[idx('new_revenue')]),backgroundColor:'#33c48d'},
           {label:'Repeat',data:R.map(r=>r[idx('repeat_revenue')]),backgroundColor:'#f5a623'}]},
 options:{scales:{x:{stacked:true,ticks:{color:'#8b93a1'}},y:{stacked:true,ticks:{color:'#8b93a1'}}},
  plugins:{legend:{labels:{color:'#c7cdd8'}}}}});
</script></body></html>"""


def write_html(path, start, end, result):
    """Standalone dashboard (data embedded, opens by double-click). Text is escaped."""
    rows = result["rows"]
    totals = {"sales_revenue": sum((r[3] or 0) for r in rows),
              "total_revenue": sum((r[6] or 0) for r in rows),
              "new_revenue": sum((r[7] or 0) for r in rows),
              "repeat_revenue": sum((r[8] or 0) for r in rows),
              "sales_count": sum((r[2] or 0) for r in rows)}
    safe = lambda obj: json.dumps(obj).replace("</", "<\\/")  # keep data from closing the <script>
    html = (_HTML.replace("__START__", start).replace("__END__", end)
            .replace("__DATA__", safe({"header": result["header"], "rows": rows}))
            .replace("__TOTALS__", safe(totals)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
