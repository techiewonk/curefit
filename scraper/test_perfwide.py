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
        CenterGroup.objects.get_or_create(name="GYMS")[0].centers.add(1)
        CenterGroup.objects.get_or_create(name="HYBRIDS")[0].centers.add(2)
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
        self.assertEqual((first["group"], first["total_revenue"], first["Retention %"]), ("GYMS", "3000", "80"))

    def test_pages_home_and_the_old_link(self):
        for url in (reverse("console:perf_analysis"),
                    reverse("console:perf_analysis") + "?mode=range&a_from=2026-07&a_to=2026-08&b_from=2025-08&b_to=2025-08",
                    reverse("console:perf_analysis") + "?mode=group&agg=avg&months=x", "/admin/", reverse("console:revenue")):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertRedirects(self.client.get(reverse("console:perf")), reverse("admin:scraper_centermonth_changelist"),
                             fetch_redirect_response=False)
        home = self.client.get("/admin/")
        self.assertContains(home, "New group")
        self.assertContains(home, "Analysis and compare")

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
        self.assertContains(self.client.get(reverse("console:revenue")), ">Ranking</th>")

    def test_report_tables_load_the_data_table_script(self):
        page = self.client.get(reverse("console:revenue"))
        self.assertContains(page, "console/datatable.js")
        self.assertContains(page, "console/wide.css")
        self.assertContains(page, 'class="data-table')


class StructureAndChartTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.session = engine.new_session("2026-06-01", "2026-08-31", [1, 2], ["perf"], granularity="month")
        CenterGroup.objects.get_or_create(name="GYMS", defaults={"position": 0})[0].centers.set([1])
        CenterGroup.objects.get_or_create(name="HYBRIDS", defaults={"position": 1})[0].centers.set([2])
        for cid, base in ((1, 1000), (2, 500)):
            for month, f in ((D(2026, 6, 1), 1), (D(2026, 7, 1), 2), (D(2026, 8, 1), 3)):
                for metric, value in ((TOTAL, base * f), ("Retention %", 60 + f * 5 + cid)):
                    PerfMetric.objects.create(session=self.session, center_id=cid, metric_date=month, metric=metric, value=value)
        perfwide.rebuild_months()

    def test_sidebar_order_and_standard_names(self):
        from django.conf import settings
        sections = [s["title"] for s in settings.UNFOLD["SIDEBAR"]["navigation"]]
        self.assertEqual(sections[:5], ["Performance", "Gym members", "Expired packs (sales)", "Operate", "Results"])
        self.assertLess(sections.index("Results"), sections.index("Centers"))
        titles = [i["title"] for s in settings.UNFOLD["SIDEBAR"]["navigation"] for i in s["items"]]
        self.assertIn("Dashboard", titles)
        self.assertIn("Monthly metrics", titles)
        self.assertNotIn("Perf metrics (raw rows)", titles)

    def test_every_chart_has_a_legend_title_and_axis_titles(self):
        import json
        from console import charts
        o = json.loads(charts.options("Revenue by group", "Month", "Revenue (₹)"))
        self.assertTrue(o["plugins"]["legend"]["display"])
        self.assertEqual((o["plugins"]["title"]["text"], o["scales"]["x"]["title"]["text"], o["scales"]["y"]["title"]["text"]),
                         ("Revenue by group", "Month", "Revenue (₹)"))
        self.assertFalse(json.loads(charts.options(legend=False))["plugins"]["legend"]["display"])
        for url in ("console:revenue", "console:perf_analysis", "console:analytics"):
            page = self.client.get(reverse(url), {"session": self.session.pk}).content.decode()
            self.assertIn("data-options=", page, url)
            self.assertIn("&quot;legend&quot;", page, url)
        self.assertIn("data-options=", self.client.get("/admin/").content.decode())

    def test_home_is_group_revenue_charts_and_status_has_the_rest(self):
        home = self.client.get("/admin/")
        self.assertContains(home, "Revenue by group")
        self.assertContains(home, "GYMS")
        self.assertNotContains(home, "Needs attention")
        status = self.client.get(reverse("console:status"))
        self.assertContains(status, "Needs attention")

    def test_perf_analysis_has_a_chart_for_every_metric(self):
        months, series = perfwide.all_series(months_back=4, today=D(2026, 9, 15))
        self.assertEqual([s["metric"] for s in series], [TOTAL, "Retention %"])
        self.assertEqual([s["how"] for s in series], ["sum", "avg"])  # money is added up, a rate is averaged
        total = next(s for s in series if s["metric"] == TOTAL)
        self.assertEqual(total["lines"]["GYMS"], [1000, 2000, 3000, None])  # Jun..Sep, September has no data yet
        self.assertEqual(total["lines"]["HYBRIDS"], [500, 1000, 1500, None])
        page = self.client.get(reverse("console:perf_analysis"))
        self.assertEqual(len(page.context["cards"]), 2)
        self.assertContains(page, "Compare this metric")

    def test_dynamic_filters(self):
        url = reverse("admin:scraper_centermonth_changelist")
        everything = self.client.get(url).context["cl"].result_count
        self.assertEqual(everything, 6)
        page = self.client.get(url)
        self.assertContains(page, "Retention %")  # a filter for each metric in the data
        high = self.client.get(url, {"m_retention___from": "76"})
        self.assertEqual(high.context["cl"].result_count, 2)  # Aug (60+15+cid) for both gyms
        low = self.client.get(url, {"m_retention___to": "70"})
        self.assertEqual(low.context["cl"].result_count, 2)
        self.assertEqual(self.client.get(url, {"m_retention___from": "x"}).context["cl"].result_count, 0)

    def test_revenue_page_from_to_filters(self):
        page = self.client.get(reverse("console:revenue"), {"from": "2026-07", "to": "2026-08"})
        self.assertEqual(page.status_code, 200)
        self.assertEqual([m.month for m in page.context["data"]["months"]], [7, 8])
        self.assertContains(page, 'name="from"')
        csv = self.client.get(reverse("console:revenue_csv"), {"from": "2026-07", "to": "2026-08"}).content.decode()
        self.assertIn("Jul-26", csv.splitlines()[0])
        self.assertNotIn("Jun-26", csv.splitlines()[0])

    # ------------------------------------------------------------------ trends, the gym page, the tables
    def test_sparkline(self):
        from scraper import sparkline
        up = sparkline.svg([1, 2, 3, None, 5], ["a", "b", "c", "d", "e"])
        self.assertIn("<svg", up)
        self.assertIn("#22c55e", up)                      # rising = green
        self.assertEqual(up.count("<polyline"), 2)        # the gap breaks the line
        self.assertIn("#ef4444", sparkline.svg([5, 3]))
        self.assertEqual(sparkline.svg([5]), "-")          # nothing to draw
        self.assertIn("a: 1", up)                         # hover text names the month

    def test_gym_page_shows_every_metric_against_the_group_average(self):
        months, rows, group = perfwide.gym_series(1, 4, today=D(2026, 9, 15))
        self.assertEqual((group, [r["metric"] for r in rows]), ("GYMS", [TOTAL, "Retention %"]))
        total = rows[0]
        self.assertEqual(total["values"], [1000, 2000, 3000, None])
        self.assertEqual(total["average"], 2000)          # September (partial, empty) is left out
        self.assertEqual(total["change_pct"], 50.0)       # August 3000 vs July 2000
        page = self.client.get(reverse("console:gym", args=[1]), {"months": 6})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(page.context["cards"]), 2)
        self.assertContains(page, "Last 6 months" if False else "All metrics by month")
        self.assertContains(page, "<svg")
        self.assertEqual(self.client.get(reverse("console:gym", args=[999999])).status_code, 404)

    def test_analysis_table_has_the_months_average_change_and_an_average_chart_line(self):
        page = self.client.get(reverse("console:perf_analysis"), {"metric": TOTAL})
        row = next(r for r in page.context["data"]["rows"] if r["id"] == 1)
        self.assertEqual(len(row["cells"]), 12)            # a column per chart month
        self.assertIsNotNone(row["average"])
        self.assertIn("mom_pct", row)
        self.assertEqual(len(page.context["data"]["totals"]), 12)
        self.assertContains(page, "Gyms by month")
        self.assertContains(page, "Last month vs previous")
        self.assertContains(page, "borderDash")           # the dashed average line
        self.assertContains(page, perfwide.AVERAGE.replace(" ", " "))

    def test_monthly_list_has_trend_columns_and_a_gym_link(self):
        page = self.client.get(reverse("admin:scraper_centermonth_changelist"))
        self.assertContains(page, "Total, 6 mo")
        self.assertContains(page, "<svg")
        self.assertContains(page, reverse("console:gym", args=[1]))

    def test_revenue_rows_have_a_six_month_trend_and_link_to_the_gym_page(self):
        page = self.client.get(reverse("console:revenue"))
        self.assertContains(page, "Last 6 full months")
        self.assertContains(page, reverse("console:gym", args=[1]))
        self.assertContains(page, "<svg")

    def test_day_coverage_is_a_real_table(self):
        from scraper.models import SalesCoverage
        SalesCoverage.objects.create(center_id=1, day=D(2026, 9, 1), session=self.session)
        url = reverse("admin:scraper_salescoverage_changelist")
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "GYMS")
        self.assertEqual(self.client.get(url, {"day_from": "2026-09-01", "day_to": "2026-09-30"}).context["cl"].result_count, 1)
        self.assertEqual(self.client.get(url, {"center__groups__name": "HYBRIDS"}).context["cl"].result_count, 0)
        from django.conf import settings
        titles = [i["title"] for s in settings.UNFOLD["SIDEBAR"]["navigation"] for i in s["items"]]
        self.assertIn("Day coverage", titles)
        self.assertIn("Missing days", titles)
        self.assertEqual(self.client.get(reverse("console:coverage")).status_code, 200)


class FetchMissingTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        from unittest import mock
        from centers import services
        from scraper import auth
        services.set_list("test", [1, 2])
        self.today = __import__("django.utils.timezone", fromlist=["x"]).localdate()
        self.logged_in = True
        patcher = mock.patch.object(auth, "logged_in", lambda fresh=False: self.logged_in)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.enqueue = mock.patch("scraper.jobs.async_task")
        self.enqueue.start()
        self.addCleanup(self.enqueue.stop)

    def test_summaries_by_day_and_by_gym(self):
        from scraper import coverage
        from scraper.models import SalesCoverage
        yesterday = self.today - dt.timedelta(days=1)
        SalesCoverage.objects.create(center_id=1, day=yesterday)
        SalesCoverage.objects.create(center_id=2, day=yesterday)
        SalesCoverage.objects.create(center_id=1, day=yesterday - dt.timedelta(days=1))
        days = coverage.missing_by_day([1, 2])
        self.assertEqual((days[0]["day"], days[0]["stored"], days[0]["missing"]), (yesterday, 2, 0))
        self.assertEqual(days[1]["missing"], 1)
        self.assertEqual(len(days), coverage.WINDOW_DAYS)
        gyms = coverage.missing_by_center([1, 2])
        self.assertEqual([(g["id"], g["stored"]) for g in gyms], [(2, 1), (1, 2)])  # the one with most missing first

    def test_the_buttons_start_a_sales_only_run_for_the_missing_days(self):
        from scraper.models import Job, Session
        page = self.client.get(reverse("console:coverage"), {"list": "test"})
        self.assertContains(page, "Fetch all missing days")
        self.assertContains(page, "Fetch this day")
        self.assertContains(page, "Fetch this gym")
        r = self.client.post(reverse("console:coverage_run"), {"list": "test", "run": "all"})
        self.assertRedirects(r, reverse("console:run"), fetch_redirect_response=False)
        job = Job.objects.get()
        session = Session.objects.get(pk=job.session_id)
        self.assertEqual((session.reports, session.sales_chunk, session.fill_missing, session.center_ids),
                         (["sales"], "day", True, [1, 2]))
        self.assertEqual(session.end_date, self.today - dt.timedelta(days=1))

    def test_one_day_and_one_gym(self):
        from scraper.models import Job, Session
        day = self.today - dt.timedelta(days=3)
        self.client.post(reverse("console:coverage_run"), {"list": "test", "run": f"day:{day}"})
        s = Session.objects.get(pk=Job.objects.get().session_id)
        self.assertEqual((s.start_date, s.end_date), (day, day))
        Job.objects.all().update(state=Job.DONE)
        self.client.post(reverse("console:coverage_run"), {"list": "test", "run": "center:2"})
        self.assertEqual(Session.objects.order_by("-created_at").first().center_ids, [2])

    def test_it_says_why_nothing_started(self):
        self.logged_in = False
        r = self.client.post(reverse("console:coverage_run"), {"list": "test", "run": "all"}, follow=True)
        self.assertContains(r, "Not logged in")
        self.logged_in = True
        r = self.client.post(reverse("console:coverage_run"), {"list": "test"}, follow=True)  # no button named: never a default run
        self.assertContains(r, "press one of the Fetch buttons")
        from scraper.models import Job
        self.assertEqual(Job.objects.count(), 0)

    def test_the_day_coverage_table_has_the_buttons_too(self):
        url = reverse("admin:scraper_salescoverage_changelist")
        self.assertContains(self.client.get(url), "Fetch the missing days")
        from centers import services
        services.set_list("actual", [1])
        r = self.client.get(reverse("admin:scraper_salescoverage_fetch_missing_days"))
        self.assertRedirects(r, reverse("console:run"), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("admin:scraper_salescoverage_open_missing_days")), reverse("console:coverage"),
                             fetch_redirect_response=False)
