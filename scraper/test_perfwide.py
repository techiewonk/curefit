import datetime as dt

from django.test import TransactionTestCase
from django.urls import reverse

from centers.models import CenterGroup
from scraper import engine, perfwide
from scraper.models import PerfMetric
from scraper.testing import FakeKronosMixin

D = dt.date
TOTAL, NEW = "1. Total Revenue", "2. New Revenue"


class PerfWideTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.month_run = engine.new_session("2025-06-01", "2026-08-31", [1, 2], ["perf"], granularity="month")
        self.day_run = engine.new_session("2026-08-01", "2026-08-31", [1, 2], ["perf"], granularity="day")
        CenterGroup.objects.create(name="GYMS").centers.add(1)
        CenterGroup.objects.create(name="HYBRIDS").centers.add(2)
        for cid, base in ((1, 1000), (2, 400)):
            for month, factor in ((D(2025, 8, 1), 1), (D(2026, 7, 1), 2), (D(2026, 8, 1), 3)):
                self.put(self.month_run, cid, month, TOTAL, base * factor)
                self.put(self.month_run, cid, month, NEW, base * factor / 4)

    def put(self, session, cid, day, metric, value):
        PerfMetric.objects.create(session=session, center_id=cid, metric_date=day, metric=metric, value=value)

    def test_metrics_become_columns_one_row_per_center_and_month(self):
        rows, columns = perfwide.wide_rows()
        self.assertEqual(columns, [TOTAL, NEW])
        self.assertEqual(len(rows), 6)  # 2 centers x 3 months
        first = rows[0]
        self.assertEqual((first["month"], first["cells"]), (D(2026, 8, 1), [3000, 750.0]))
        self.assertEqual(len(perfwide.wide_rows(group="GYMS")[0]), 3)
        self.assertEqual(len(perfwide.wide_rows(since=D(2026, 7, 1))[0]), 4)

    def test_a_month_split_run_beats_day_rows_and_day_rows_are_added_when_alone(self):
        for day in (3, 4, 5):
            self.put(self.day_run, 1, D(2026, 8, day), TOTAL, 10)   # August already has a month figure
        self.put(self.day_run, 1, D(2026, 9, 1), TOTAL, 5)
        self.put(self.day_run, 1, D(2026, 9, 2), TOTAL, 7)           # September only has day rows
        values = perfwide.month_values()
        self.assertEqual(values[(1, D(2026, 8, 1))][TOTAL], 3000)
        self.assertEqual(values[(1, D(2026, 9, 1))][TOTAL], 12)

    def test_analysis_modes(self):
        today = D(2026, 9, 15)
        mom = perfwide.analyse(TOTAL, mode="mom", month=D(2026, 8, 1), today=today)
        self.assertEqual(mom["total_now"], 4200)
        row = next(r for r in mom["rows"] if r["id"] == 1)
        self.assertEqual((row["now"], row["before"], row["pct"]), (3000, 2000, 50.0))
        yoy = perfwide.analyse(TOTAL, mode="yoy", month=D(2026, 8, 1), today=today)
        self.assertEqual(next(r for r in yoy["rows"] if r["id"] == 1)["pct"], 200.0)
        rng = perfwide.analyse(TOTAL, mode="range", a=(D(2026, 7, 1), D(2026, 8, 1)), b=(D(2025, 8, 1), D(2025, 8, 1)),
                               today=today)
        self.assertEqual(next(r for r in rng["rows"] if r["id"] == 1)["now"], 5000)
        grp = perfwide.analyse(TOTAL, mode="group", month=D(2026, 8, 1), today=today)
        self.assertEqual(len(grp["rows"]), 2)
        self.assertEqual(set(grp["lines"]), {"GYMS", "HYBRIDS"})
        avg = perfwide.analyse(TOTAL, group="GYMS", agg="avg", month=D(2026, 8, 1), today=today)
        self.assertEqual(avg["lines"]["GYMS"][-2], 3000)

    def test_pages_download_home_and_sidebar_sections(self):
        for url in (reverse("console:perf"), reverse("console:perf") + "?group=GYMS&from=2026-07&q=cult",
                    reverse("console:perf_analysis"), reverse("console:perf_analysis") + "?mode=range&a_from=2026-07&a_to=2026-08&b_from=2025-08&b_to=2025-08",
                    reverse("console:perf_analysis") + "?mode=group&agg=avg&months=x", "/admin/", reverse("console:revenue")):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        r = self.client.get(reverse("console:perf_csv"), {"group": "GYMS"})
        header = r.content.decode().splitlines()[0]
        self.assertEqual(header, "center_id,center,group,month," + TOTAL + "," + NEW)
        self.assertIn("spreadsheetml", self.client.get(reverse("console:perf_csv"), {"format": "xlsx"})["Content-Type"])
        home = self.client.get("/admin/")
        self.assertContains(home, "New group")
        self.assertContains(self.client.get("/admin/"), "Perf analysis and compare")

    def test_groups_by_month_table_and_download(self):
        from scraper import revenue
        t = revenue.group_month_table(today=D(2026, 9, 15))
        self.assertEqual(len(t["months"]), 12)
        self.assertEqual([r["group"] for r in t["rows"]], ["GYMS", "HYBRIDS"])
        aug = t["months"].index(D(2026, 8, 1))
        self.assertEqual([r["cells"][aug] for r in t["rows"]], [3000, 1200])
        self.assertEqual(t["total"][aug], 4200)
        self.assertNotIn("average", [None])
        headers, rows = revenue.group_month_download(t)
        self.assertEqual((headers[:2], headers[-1], len(rows)), (["group", "gyms"], "average", 2))
        page = self.client.get(reverse("console:revenue"))
        self.assertContains(page, "Groups by month")
        csv = self.client.get(reverse("console:revenue_csv"), {"part": "groups"})
        self.assertTrue(csv.content.decode().startswith("group,gyms,"))
