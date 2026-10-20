"""
Bring everything the old scripts produced into the Django database.

    python manage.py import_legacy                       # curefit.db, centers.json, lists, runs/
    python manage.py import_legacy --legacy-db a.db b.db # several old databases
    python manage.py import_legacy --replace             # re-import data even if already there

Safe to run again: old databases are opened read-only, sessions and results are matched by id,
and data tables are skipped for any session that already has rows (unless --replace).
"""
import csv
import datetime as dt
import json
import sqlite3
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from centers.models import Center, CenterList
from centers.services import set_list, upsert_from_kronos
from scraper.models import (CardColumns, CardRow, GymMember, PerfMetric, SalesRecord,
                            SectionResult, Session)

BATCH = 2000


def _aware(value):
    """Old timestamps are naive local ISO strings."""
    if not value:
        return timezone.now()
    try:
        d = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return timezone.now()
    return timezone.make_aware(d) if timezone.is_naive(d) else d


def _date(value):
    return dt.date.fromisoformat(str(value)[:10])


def _float(value):
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    while True:
        batch = cur.fetchmany(5000)
        if not batch:
            return
        yield from batch


class Command(BaseCommand):
    help = "Import the old curefit.db, centers.json, center lists and runs/ into Django."

    def add_arguments(self, p):
        p.add_argument("--legacy-db", nargs="+", default=[str(settings.BASE_DIR / "curefit.db")])
        p.add_argument("--runs", default=str(settings.BASE_DIR / "runs"))
        p.add_argument("--replace", action="store_true",
                       help="re-import data tables even for sessions that already have rows")

    # ------------------------------------------------------------------ main
    def handle(self, *args, **o):
        self.replace = o["replace"]
        self.base = Path(settings.BASE_DIR)
        self.import_centers()
        self.import_lists()
        for path in o["legacy_db"]:
            if Path(path).exists():
                self.import_db(Path(path))
            else:
                self.stderr.write(f"skip (not found): {path}")
        self.import_session_files(Path(o["runs"]))
        self.summary()

    def say(self, msg):
        self.stdout.write(msg)

    # --------------------------------------------------------------- centers
    def import_centers(self):
        f = self.base / "centers.json"
        if not f.exists():
            return self.say("centers.json not found — centers will be created as needed")
        data = json.loads(f.read_text(encoding="utf-8"))
        diff = upsert_from_kronos(data.get("centers", []), log=False)
        self.say(f"centers: {diff['total']} from centers.json "
                 f"({len(diff['added'])} new, {len(diff['state_changed'])} state changes)")

    def import_lists(self):
        for name, fname in (("actual", "centers_to_process.csv"), ("test", "centers_test.csv")):
            f = self.base / fname
            if not f.exists():
                continue
            if CenterList.objects.filter(name=name, centers__isnull=False).exists() and not self.replace:
                self.say(f"list {name}: already has centers, left as is")
                continue
            with f.open(newline="", encoding="utf-8-sig") as fh:
                ids = [r["center_id"].strip() for r in csv.DictReader(fh)
                       if (r.get("center_id") or "").strip().isdigit()]
            res = set_list(name, ids)
            self.say(f"list {name}: {res['count']} centers ({len(res['inactive'])} not ACTIVE)")

    # ----------------------------------------------------------- old database
    def import_db(self, path):
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = lambda t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        has_sid = "session_id" in cols("sales")
        sid_expr = "session_id" if has_sid else "'legacy'"
        self.say(f"\n{path.name}: {'session layout' if has_sid else 'old layout (no sessions)'}")

        self.ensure_centers(conn, tables)
        sessions = self.ensure_sessions(conn, tables, sid_expr, has_sid)

        # per-center results (the old scrape_log)
        n = 0
        for sid, start, end, cid, report, count, at in _rows(
                conn, f"SELECT {sid_expr}, start_date, end_date, center_id, report, row_count, "
                      f"scraped_at FROM scrape_log"):
            sess = sessions[(sid, start, end)]
            SectionResult.objects.update_or_create(
                session=sess, center_id=int(cid), report=report,
                defaults={"status": "ok" if count else "empty", "row_count": count or 0,
                          "scraped_at": _aware(at)})
            n += 1
        self.say(f"  results: {n}")

        self.import_table(conn, "sales", SalesRecord, sessions, sid_expr, [
            ("user_id", str), ("name", None), ("phone", None), ("pack_name", None),
            ("member_end_date", None), ("active_last_30d", None), ("renewed", None),
            ("revenue", _float)])
        self.import_table(conn, "perf_metrics", PerfMetric, sessions, sid_expr, [
            ("metric_date", None), ("metric", None), ("value", _float)])
        if "gym_members" in tables:
            self.import_table(conn, "gym_members", GymMember, sessions, sid_expr, [
                ("user_id", str), ("username", None), ("pack_name", None),
                ("final_amount_paid", _float), ("purchase_date", None), ("pack_start_date", None),
                ("pack_end_date", None), ("status", None), ("offline_vs_online", None),
                ("new_vs_repeat", None), ("class_level", None), ("last_class_date", None)])
        if "card_rows" in tables:
            self.import_cards(conn, sessions, sid_expr)
        if "card_columns" in tables:
            for report, columns in conn.execute("SELECT report, columns FROM card_columns"):
                CardColumns.objects.update_or_create(report=report,
                                                     defaults={"columns": json.loads(columns)})
        conn.close()

    def ensure_centers(self, conn, tables):
        known = set(Center.objects.values_list("center_id", flat=True))
        found = {}
        for t in ("sales", "perf_metrics", "gym_members", "card_rows"):
            if t in tables and "center_name" in [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]:
                for cid, name in conn.execute(f"SELECT center_id, MAX(center_name) FROM {t} GROUP BY center_id"):
                    found.setdefault(int(cid), name or "")
        for cid, in conn.execute("SELECT DISTINCT center_id FROM scrape_log"):
            found.setdefault(int(cid), "")
        stubs = [Center(center_id=c, name=found[c]) for c in found if c not in known]
        Center.objects.bulk_create(stubs)
        if stubs:
            self.say(f"  {len(stubs)} centers not in centers.json were created as stubs")

    def ensure_sessions(self, conn, tables, sid_expr, has_sid):
        """Map (session_id, start, end) in the old tables -> Session rows."""
        keys = set()
        for t in ("scrape_log", "sales", "perf_metrics"):
            for k in conn.execute(f"SELECT DISTINCT {sid_expr}, start_date, end_date FROM {t}"):
                keys.add(tuple(k))
        info = {}
        if "sessions" in tables:
            for r in conn.execute("SELECT session_id, start_date, end_date, status, created_at, "
                                  "finished_at, folder, centers, reports, coverage, missing FROM sessions"):
                info[r[0]] = r
        out = {}
        for sid, start, end in sorted(keys, key=str):
            name = f"legacy-{start}_{end}" if sid in (None, "legacy") else sid
            times = conn.execute(
                f"SELECT MIN(scraped_at), MAX(scraped_at) FROM scrape_log WHERE {sid_expr}=? "
                f"AND start_date=? AND end_date=?", (sid, start, end)).fetchone()
            ids = [str(r[0]) for r in conn.execute(
                f"SELECT DISTINCT center_id FROM scrape_log WHERE {sid_expr}=? AND start_date=? "
                f"AND end_date=?", (sid, start, end))]
            reps = [r[0] for r in conn.execute(
                f"SELECT DISTINCT report FROM scrape_log WHERE {sid_expr}=? AND start_date=? "
                f"AND end_date=?", (sid, start, end))]
            defaults = {"start_date": _date(start), "end_date": _date(end),
                        "status": Session.LEGACY, "created_at": _aware(times[0]),
                        "finished_at": _aware(times[1]), "reports": reps,
                        "center_ids": sorted(ids, key=int)}
            if sid in info:
                r = info[sid]
                defaults.update(status=r[3], created_at=_aware(r[4]),
                                finished_at=_aware(r[5]) if r[5] else None, folder=r[6] or "",
                                center_ids=json.loads(r[7] or "[]"),
                                reports=json.loads(r[8] or "[]") or reps,
                                coverage=json.loads(r[9]) if r[9] else None,
                                missing=json.loads(r[10] or "{}"))
            sess, created = Session.objects.update_or_create(session_id=name, defaults=defaults)
            out[(sid, start, end)] = sess
            self.say(f"  session {name}: {'new' if created else 'updated'} "
                     f"({len(ids)} centers, reports {', '.join(reps)})")
        return out

    def import_table(self, conn, table, model, sessions, sid_expr, fields):
        names = [f for f, _ in fields]
        total = 0
        for (sid, start, end), sess in sessions.items():
            if model.objects.filter(session=sess).exists():
                if not self.replace:
                    continue
                model.objects.filter(session=sess).delete()
            sql = (f"SELECT center_id, {', '.join(names)}, scraped_at FROM {table} "
                   f"WHERE {sid_expr}=? AND start_date=? AND end_date=?")
            batch = []
            with transaction.atomic():
                for row in _rows(conn, sql, (sid, start, end)):
                    obj = {"session": sess, "center_id": int(row[0]),
                           "scraped_at": _aware(row[-1])}
                    for (name, conv), value in zip(fields, row[1:-1]):
                        obj[name] = conv(value) if (conv and value is not None) else value
                    batch.append(model(**obj))
                    if len(batch) >= BATCH:
                        model.objects.bulk_create(batch)
                        total += len(batch)
                        batch = []
                if batch:
                    model.objects.bulk_create(batch)
                    total += len(batch)
        self.say(f"  {table}: {total} rows imported")

    def import_cards(self, conn, sessions, sid_expr):
        total = 0
        for (sid, start, end), sess in sessions.items():
            if CardRow.objects.filter(session=sess).exists():
                if not self.replace:
                    continue
                CardRow.objects.filter(session=sess).delete()
            batch = []
            with transaction.atomic():
                for rep, cid, num, data, at in _rows(
                        conn, f"SELECT report, center_id, row_num, data, scraped_at FROM card_rows "
                              f"WHERE {sid_expr}=? AND start_date=? AND end_date=?", (sid, start, end)):
                    batch.append(CardRow(session=sess, center_id=int(cid), report=rep, row_num=num,
                                         data=json.loads(data), scraped_at=_aware(at)))
                    if len(batch) >= BATCH:
                        CardRow.objects.bulk_create(batch)
                        total += len(batch)
                        batch = []
                if batch:
                    CardRow.objects.bulk_create(batch)
                    total += len(batch)
        self.say(f"  card_rows: {total} rows imported")

    # ------------------------------------------------------- session folders
    def import_session_files(self, runs):
        """Sessions known only from runs/**/session.json (no database rows)."""
        n = 0
        for f in runs.glob("*/*/session.json"):
            info = json.loads(f.read_text(encoding="utf-8"))
            sid = info["session_id"]
            defaults = {
                "start_date": _date(info["start"]), "end_date": _date(info["end"]),
                "status": info.get("status", "incomplete"),
                "created_at": _aware(info.get("created_at")),
                "finished_at": _aware(info["finished_at"]) if info.get("finished_at") else None,
                "folder": str(f.parent), "reports": info.get("reports", []),
                "center_ids": info.get("centers", []), "missing": info.get("missing", {})}
            sess, created = Session.objects.get_or_create(session_id=sid, defaults=defaults)
            if not created and not sess.folder:
                sess.folder = str(f.parent)
                sess.save(update_fields=["folder"])
            n += created
        self.say(f"\nsession folders: {n} new session(s) from {runs}")

    def summary(self):
        self.say("\n--- totals in the Django database ---")
        for label, model in (("centers", Center), ("sessions", Session), ("section results", SectionResult),
                             ("sales rows", SalesRecord), ("perf rows", PerfMetric),
                             ("gym rows", GymMember), ("card rows", CardRow)):
            self.say(f"{label:>16}: {model.objects.count()}")
        for lst in CenterList.objects.all():
            self.say(f"{'list ' + lst.name:>16}: {lst.centers.count()}")
