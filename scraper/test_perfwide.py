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
        from scraper.models import CenterMonth
        self.assertEqual(perfwide.rebuild_months(), 6)  # 2 centers x 3 months
        self.assertEqual(perfwide.rebuild_months(), 6)  # repeating changes nothing
        self.assertEqual(CenterMonth.objects.count(), 6)
        row = CenterMonth.objects.get(center_id=1, month=D(2026, 8, 1))
        self.assertEqual((row.total_revenue, row.new_revenue, row.repeat_revenue), (3000, 750.0, None))
        self.assertEqual(row.metrics, {TOTAL: 3000, NEW: 750.0})
        self.put(self.month_run, 1, D(2026, 8, 1), "Retention %", 80)
        self.assertEqual(perfwide.rebuild_months(self.month_run), 6)  # only that session's centers and months
        self.assertEqual(CenterMonth.objects.get(center_id=1, month=D(2026, 8, 1)).metrics["Retention %"], 80)
        self.assertEqual(perfwide.extra_metric_names(), ["Retention %"])

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

    def test_the_monthly_table_is_a_real_admin_list_with_filters_and_export(self):
        from scraper.resources import CenterMonthResource
        self.put(self.month_run, 1, D(2026, 8, 1), "Retention %", 80)
        perfwide.rebuild_months()
        url = reverse("admin:scraper_centermonth_changelist")
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Retention %")  # a column per metric
        self.assertEqual(self.client.get(url, {"total_revenue_from": "2500"}).context["cl"].result_count, 1)
        self.assertEqual(self.client.get(url, {"center__groups__name": "GYMS"}).context["cl"].result_count, 3)
        self.assertEqual(self.client.get(url, {"month_from": "2026-08-01", "month_to": "2026-08-31"}).context["cl"].result_count, 2)
        self.assertEqual(self.client.get(url + "?q=zzz").context["cl"].result_count, 0)
        self.assertEqual(self.client.get(reverse("admin:scraper_centermonth_export")).status_code, 200)
        data = CenterMonthResource().export()
        self.assertEqual(data.headers, ["center_id", "center", "group", "month", "total_revenue", "new_revenue",
                                        "repeat_revenue", "Retention %"])
        first = next(r for r in data.dict if r["center_id"] == "1" and r["month"] == "Aug-26")
        self.assertEqual((first["group"], first["total_revenue"], first["Retention %"]), ("GYMS", "3000.0", 80))

    def test_pages_home_and_the_old_link(self):
        for url in (reverse("console:perf_analysis"),
                    reverse("console:perf_analysis") + "?mode=range&a_from=2026-07&a_to=2026-08&b_from=2025-08&b_to=2025-08",
                    reverse("console:perf_analysis") + "?mode=group&agg=avg&months=x", "/admin/", reverse("console:revenue")):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertRedirects(self.client.get(reverse("console:perf")), reverse("admin:scraper_centermonth_changelist"),
                             fetch_redirect_response=False)
        home = self.client.get("/admin/")
        self.assertContains(home, "New group")
        self.assertContains(home, "Perf analysis and compare")

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

    def test_a_ranking_column_for_each_of_the_last_six_months(self):
        from scraper import revenue
        data = revenue.matrix(months=0, today=D(2026, 9, 15))
        self.assertEqual(len(data["rank_months"]), 6)
        gyms = next(s for s in data["sections"] if s["group"] == "GYMS")
        self.assertEqual(len(gyms["rows"][0]["ranks"]), 6)
        headers, rows = revenue.table(data)
        self.assertEqual([h for h in headers if h.startswith("ranking")][-2:], ["ranking Sep-26", "ranking"])
        self.assertEqual(len(headers), len(rows[0]))
        self.assertContains(self.client.get(reverse("console:revenue")), "Ranking ")

    def test_report_tables_load_the_data_table_script(self):
        page = self.client.get(reverse("console:revenue"))
        self.assertContains(page, "console/datatable.js")
        self.assertContains(page, "console/wide.css")
        self.assertContains(page, 'class="data-table')
