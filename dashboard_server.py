"""
Live dashboard for the Curefit scraper — one page that auto-reads curefit.db.

Run it and it opens a browser showing the latest data straight from SQLite.
Pick any scraped period from the dropdown; hit Reload to see fresh data after a scrape.

    python dashboard_server.py
    python dashboard_server.py --db curefit.db --port 8000

No extra dependencies (standard library only).
"""
import argparse
import json
import os
import sqlite3
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import scrape_sales as S

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "curefit.db")


def db():
    conn = sqlite3.connect(DB_PATH)
    return conn


def list_periods():
    """One entry per scrape session (legacy rows from before sessions show as 'legacy')."""
    conn = db()
    try:
        rows = conn.execute("""
            SELECT session_id, start_date, end_date, COUNT(DISTINCT center_id) AS centers,
                   MAX(scraped_at) AS last_at
            FROM scrape_log WHERE row_count>0
            GROUP BY session_id, start_date, end_date
            ORDER BY last_at DESC
        """).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [{"session": r[0], "start": r[1], "end": r[2], "centers": r[3], "last": r[4]}
            for r in rows]


def sessions_payload():
    """Admin view: every session with status, coverage and what is still missing."""
    conn = db()
    try:
        rows = conn.execute("""
            SELECT session_id, start_date, end_date, status, created_at, finished_at, folder,
                   centers, reports, coverage, missing FROM sessions ORDER BY created_at DESC
        """).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    out = []
    for r in rows:
        out.append({"session": r[0], "start": r[1], "end": r[2], "status": r[3],
                    "created_at": r[4], "finished_at": r[5], "folder": r[6],
                    "centers": len(json.loads(r[7] or "[]")), "reports": json.loads(r[8] or "[]"),
                    "coverage": json.loads(r[9] or "null"), "missing": json.loads(r[10] or "{}")})
    return out


def analysis_payload(sid, start, end):
    conn = db()
    metrics = [r[0] for r in conn.execute(
        "SELECT DISTINCT metric FROM perf_metrics WHERE session_id=? AND start_date=? "
        "AND end_date=? ORDER BY metric", (sid, start, end))]
    analysis = S.build_analysis(conn, start, end, metrics, sid)
    rows = analysis["rows"]

    # per-center extras: active rate + average ticket size
    extra = {}
    for cid, cnt, act, rev in conn.execute(
        """SELECT center_id, COUNT(*),
                  SUM(CASE WHEN active_last_30d='Yes' THEN 1 ELSE 0 END),
                  COALESCE(SUM(revenue),0)
           FROM sales WHERE session_id=? AND start_date=? AND end_date=?
           GROUP BY center_id""", (sid, start, end)):
        extra[str(cid)] = {
            "active_count": act or 0,
            "active_rate": round(100.0 * (act or 0) / cnt, 1) if cnt else 0,
            "avg_ticket": round(rev / cnt, 0) if cnt else 0,
        }

    # pack-mix breakdown across the selected centers
    packs = [{"pack": p, "count": c, "revenue": round(r or 0, 0)}
             for p, c, r in conn.execute(
                 """SELECT pack_name, COUNT(*), SUM(revenue)
                    FROM sales WHERE session_id=? AND start_date=? AND end_date=?
                    GROUP BY pack_name ORDER BY COUNT(*) DESC""", (sid, start, end))]

    sc = sum((r[2] or 0) for r in rows)
    sr = sum((r[3] or 0) for r in rows)
    ac = sum(e["active_count"] for e in extra.values())
    totals = {
        "centers": len(rows),
        "sales_count": sc,
        "sales_revenue": sr,
        "total_revenue": sum((r[6] or 0) for r in rows),
        "new_revenue": sum((r[7] or 0) for r in rows),
        "repeat_revenue": sum((r[8] or 0) for r in rows),
        "avg_ticket": round(sr / sc, 0) if sc else 0,
        "active_rate": round(100.0 * ac / sc, 1) if sc else 0,
    }
    conn.close()
    return {"header": analysis["header"], "rows": rows, "extra": extra,
            "packs": packs, "totals": totals, "start": start, "end": end}


def sales_detail(sid, start, end, center_id):
    conn = db()
    cols = ["user_id", "name", "phone", "pack_name", "member_end_date",
            "active_last_30d", "renewed", "revenue"]
    rows = conn.execute(
        f"SELECT {','.join(cols)} FROM sales WHERE session_id=? AND start_date=? AND end_date=? "
        f"AND center_id=? ORDER BY revenue DESC NULLS LAST, name",
        (sid, start, end, str(center_id))).fetchall()
    conn.close()
    return {"header": cols, "rows": rows}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _send(self, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                self._send(PAGE, "text/html; charset=utf-8")
            elif u.path == "/api/periods":
                self._send(json.dumps(list_periods()))
            elif u.path == "/api/sessions":
                self._send(json.dumps(sessions_payload()))
            elif u.path == "/api/analysis":
                self._send(json.dumps(analysis_payload(q["session"][0], q["start"][0], q["end"][0])))
            elif u.path == "/api/sales":
                self._send(json.dumps(sales_detail(q["session"][0], q["start"][0], q["end"][0],
                                                   q["center_id"][0])))
            else:
                self.send_error(404)
        except Exception as e:
            self._send(json.dumps({"error": str(e)}))


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Curefit Live Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
 body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
 header{padding:18px 28px;background:#161a22;border-bottom:1px solid #262b36;display:flex;
        align-items:center;gap:18px;flex-wrap:wrap}
 h1{margin:0;font-size:19px} .sub{color:#8b93a1;font-size:13px}
 select,button{background:#1c212b;color:#e6e6e6;border:1px solid #333a48;border-radius:7px;
        padding:7px 10px;font-size:13px}
 button{cursor:pointer} button:hover{background:#252c39}
 .wrap{padding:22px 28px;max-width:1250px;margin:0 auto}
 .cards{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:22px}
 .card{background:#161a22;border:1px solid #262b36;border-radius:10px;padding:14px 18px;min-width:150px}
 .card .lbl{color:#8b93a1;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
 .card .val{font-size:22px;font-weight:600;margin-top:6px}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-bottom:22px}
 .panel{background:#161a22;border:1px solid #262b36;border-radius:10px;padding:16px}
 .panel h2{font-size:13px;margin:0 0 12px;color:#c7cdd8;text-transform:uppercase;letter-spacing:.04em}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:7px 9px;text-align:right;border-bottom:1px solid #262b36}
 th:first-child,td:first-child{text-align:left}
 th{color:#8b93a1;font-weight:600;position:sticky;top:0;background:#161a22}
 tbody tr{cursor:pointer} tbody tr:hover td{background:#1c212b}
 tr.sel td{background:#20304a}
 @media(max-width:900px){.grid{grid-template-columns:1fr}}
 .muted{color:#8b93a1}
</style></head><body>
<header>
  <h1>Curefit Live Dashboard</h1>
  <span class="sub">reads directly from curefit.db</span>
  <span style="flex:1"></span>
  <label class="sub">Session&nbsp;</label>
  <select id="period"></select>
  <button onclick="load()">↻ Reload</button>
</header>
<div class="wrap">
  <div class="cards" id="cards"></div>
  <div class="grid">
    <div class="panel"><h2>Revenue by center</h2><canvas id="revChart" height="230"></canvas></div>
    <div class="panel"><h2>New vs Repeat revenue</h2><canvas id="mixChart" height="230"></canvas></div>
  </div>
  <div class="panel"><h2>Per-center analysis <span class="muted">(click a row for member detail)</span></h2>
    <div style="overflow:auto;max-height:430px"><table id="tbl"></table></div></div>
  <div class="grid" style="margin-top:22px">
    <div class="panel"><h2>Pack mix — revenue share</h2><canvas id="packChart" height="240"></canvas></div>
    <div class="panel"><h2>Pack breakdown</h2>
      <div style="overflow:auto;max-height:300px"><table id="packTbl"></table></div></div>
  </div>
  <div class="panel" id="detailPanel" style="display:none;margin-top:22px">
    <h2 id="detailTitle"></h2>
    <div style="overflow:auto;max-height:430px"><table id="detail"></table></div>
  </div>
</div>
<script>
const inr=n=>n==null?'-':'₹'+Number(n).toLocaleString('en-IN');
const num=n=>n==null?'-':Number(n).toLocaleString('en-IN');
let revChart, mixChart, packChart, CURRENT;

async function j(u){const r=await fetch(u);return r.json();}

async function initPeriods(){
  const ps=await j('/api/periods');
  const sel=document.getElementById('period');
  if(!ps.length){sel.innerHTML='<option>no data yet</option>';return;}
  sel.innerHTML=ps.map(p=>`<option value="${p.session}|${p.start}|${p.end}">${p.start} → ${p.end} · session ${p.session} (${p.centers} centers)</option>`).join('');
  load();
}

async function load(){
  const v=document.getElementById('period').value;
  if(!v||v==='no data yet')return;
  const [session,start,end]=v.split('|');
  CURRENT={session,start,end};
  const d=await j(`/api/analysis?session=${session}&start=${start}&end=${end}`);
  renderCards(d.totals); renderTable(d); renderCharts(d); renderPacks(d.packs);
  document.getElementById('detailPanel').style.display='none';
}

function renderCards(t){
  const cards=[['Centers',num(t.centers)],['Sales count',num(t.sales_count)],
    ['Sales revenue',inr(t.sales_revenue)],['Total revenue',inr(t.total_revenue)],
    ['New revenue',inr(t.new_revenue)],['Repeat revenue',inr(t.repeat_revenue)],
    ['Avg ticket',inr(t.avg_ticket)],['Active rate',t.active_rate+'%']];
  document.getElementById('cards').innerHTML=cards.map(c=>
    `<div class="card"><div class="lbl">${c[0]}</div><div class="val">${c[1]}</div></div>`).join('');
}

function renderTable(d){
  const H=d.header,R=d.rows,ex=d.extra||{},money=[3,6,7,8];
  const head=H.concat(['active_%','avg_ticket']);
  let t='<thead><tr>'+head.map(h=>`<th>${h}</th>`).join('')+'</tr></thead><tbody>';
  t+=R.map(r=>{
     const e=ex[String(r[0])]||{};
     const base=r.map((v,i)=>`<td>${v==null?'':(money.includes(i)?inr(v):v)}</td>`).join('');
     const add=`<td>${e.active_rate==null?'':e.active_rate+'%'}</td><td>${inr(e.avg_ticket)}</td>`;
     return `<tr onclick="showDetail('${r[0]}','${(r[1]||'').replace(/'/g,'')}',this)">${base}${add}</tr>`;
  }).join('');
  document.getElementById('tbl').innerHTML=t+'</tbody>';
}

function renderPacks(packs){
  packs=packs||[];
  let t='<thead><tr><th>Pack</th><th>Sales</th><th>Revenue</th></tr></thead><tbody>';
  t+=packs.map(p=>`<tr><td>${p.pack||''}</td><td>${num(p.count)}</td><td>${inr(p.revenue)}</td></tr>`).join('');
  document.getElementById('packTbl').innerHTML=t+'</tbody>';
  const top=packs.filter(p=>p.revenue>0).slice(0,8);
  const palette=['#4f8cff','#33c48d','#f5a623','#e5567b','#9b6dff','#2ec5c5','#c9d14a','#ff8a5c'];
  packChart&&packChart.destroy();
  packChart=new Chart(document.getElementById('packChart'),{type:'doughnut',
    data:{labels:top.map(p=>p.pack),datasets:[{data:top.map(p=>p.revenue),backgroundColor:palette}]},
    options:{plugins:{legend:{position:'right',labels:{color:'#c7cdd8',boxWidth:12,font:{size:11}}}}}});
}

function renderCharts(d){
  const H=d.header,R=d.rows,idx=n=>H.indexOf(n);
  const names=R.map(r=>r[1]||r[0]);
  const opt=st=>({plugins:{legend:{labels:{color:'#c7cdd8'}}},
    scales:{x:{stacked:st,ticks:{color:'#8b93a1'}},y:{stacked:st,ticks:{color:'#8b93a1'}}}});
  revChart&&revChart.destroy(); mixChart&&mixChart.destroy();
  revChart=new Chart(revChart_ctx(),{type:'bar',data:{labels:names,
    datasets:[{label:'Total revenue',data:R.map(r=>r[idx('total_revenue')]),backgroundColor:'#4f8cff'}]},
    options:{...opt(false),plugins:{legend:{display:false}}}});
  mixChart=new Chart(mixChart_ctx(),{type:'bar',data:{labels:names,
    datasets:[{label:'New',data:R.map(r=>r[idx('new_revenue')]),backgroundColor:'#33c48d'},
              {label:'Repeat',data:R.map(r=>r[idx('repeat_revenue')]),backgroundColor:'#f5a623'}]},
    options:opt(true)});
}
const revChart_ctx=()=>document.getElementById('revChart');
const mixChart_ctx=()=>document.getElementById('mixChart');

async function showDetail(cid,cname,rowEl){
  document.querySelectorAll('#tbl tr').forEach(t=>t.classList.remove('sel'));
  rowEl.classList.add('sel');
  const d=await j(`/api/sales?session=${CURRENT.session}&start=${CURRENT.start}&end=${CURRENT.end}&center_id=${cid}`);
  const money=[7];
  let t='<thead><tr>'+d.header.map(h=>`<th>${h}</th>`).join('')+'</tr></thead><tbody>';
  t+=d.rows.map(r=>'<tr>'+r.map((v,i)=>`<td>${v==null?'':(money.includes(i)?inr(v):v)}</td>`).join('')+'</tr>').join('');
  document.getElementById('detail').innerHTML=t+'</tbody>';
  document.getElementById('detailTitle').textContent=`Member detail — ${cname} (${d.rows.length} sales)`;
  document.getElementById('detailPanel').style.display='block';
}

initPeriods();
</script></body></html>"""


def main():
    global DB_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    DB_PATH = args.db if os.path.isabs(args.db) else os.path.join(HERE, args.db)

    # databases from before sessions need the session columns (rows become 'legacy')
    conn = sqlite3.connect(DB_PATH)
    S.init_db(conn)
    conn.close()

    url = f"http://localhost:{args.port}/"
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Live dashboard: {url}   (DB: {DB_PATH})")
    print("Press Ctrl+C to stop.")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
