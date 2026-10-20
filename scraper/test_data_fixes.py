"""Tests for the data fixes: month figures (mixed splits, partial months), merging, repairs, checks, helpers.

Every date is fixed: sessions get an explicit created_at and the pages an explicit `today`."""
import datetime as dt
import math
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TransactionTestCase
from django.utils import timezone

from centers.models import CenterGroup
from scraper import analysis, checks, customers, engine, masters, numbers, perfwide, repairs, revenue, sparkline
from scraper.models import (AutoRun, CenterMonth, Customer, GymMember, PerfMetric, RepairLog, SalesCoverage,
                            SalesPack, SalesRecord, Session)
from scraper.testing import FakeKronosMixin

D = dt.date
TOTAL = "1. Total Revenue"
TODAY = D(2026, 10, 15)


def at(day):
    return timezone.make_aware(dt.datetime(day.year, day.month, day.day, 12))


def make_session(start, end, gran=Session.MONTH, fetched=D(2026, 10, 1), cids=(1, 2), reports=("perf",), **kw):
    s = engine.new_session(start, end, list(cids), list(reports), granularity=gran, **kw)
    Session.objects.filter(pk=s.pk).update(created_at=at(fetched))
    return Session.objects.get(pk=s.pk)


def put(session, cid, day, value, metric=TOTAL):
    PerfMetric.objects.create(session=session, center_id=cid, metric_date=day, metric=metric, value=value)


class MonthFigureTests(FakeKronosMixin, TransactionTestCase):
    def test_day_and_week_rows_of_one_month_are_never_mixed(self):  # G8
        weeks = make_session(D(2026, 8, 1), D(2026, 8, 31), Session.WEEK, fetched=D(2026, 9, 5))
        for monday in (D(2026, 7, 27), D(2026, 8, 3), D(2026, 8, 10), D(2026, 8, 17), D(2026, 8, 24), D(2026, 8, 31)):
            put(weeks, 1, monday, 70)
        days = make_session(D(2026, 8, 1), D(2026, 8, 31), Session.DAY, fetched=D(2026, 9, 6))  # newer
        for d in range(1, 32):
            put(days, 1, D(2026, 8, d), 10)
        values = perfwide.month_values()
        self.assertEqual(values[(1, D(2026, 8, 1))][TOTAL], 310)  # the day rows alone, not day + week rows
        # the week of 27 July was fetched for a period starting 1 August: its figure is August's, not July's
        self.assertNotIn((1, D(2026, 7, 1)), values)

    def test_a_week_across_the_month_end_is_split_by_days_and_marked_estimated(self):  # G8
        weeks = make_session(D(2026, 7, 27), D(2026, 8, 2), Session.WEEK)
        put(weeks, 1, D(2026, 7, 27), 70)  # 27 July - 2 August: five days in July, two in August
        figures = perfwide.month_figures()
        july, august = figures[(1, D(2026, 7, 1))][TOTAL], figures[(1, D(2026, 8, 1))][TOTAL]
        self.assertEqual((round(july.value), round(august.value)), (50, 20))
        self.assertTrue(july.estimated and august.estimated)
        self.assertEqual((july.covered, august.covered), (5, 2))

    def test_coverage_is_clamped_to_the_period_and_the_fetch_day(self):  # G9 / G44
        created = at(D(2026, 8, 10))
        self.assertEqual(perfwide._span(D(2026, 8, 1), Session.MONTH, D(2026, 8, 1), D(2026, 8, 31), created),
                         (D(2026, 8, 1), D(2026, 8, 10)))
        self.assertEqual(perfwide._span(D(2026, 7, 27), Session.WEEK, D(2026, 8, 1), D(2026, 8, 31), created),
                         (D(2026, 8, 1), D(2026, 8, 2)))
        early = make_session(D(2026, 8, 1), D(2026, 8, 31), fetched=D(2026, 8, 10))
        put(early, 1, D(2026, 8, 1), 3000)
        figure = perfwide.month_figures()[(1, D(2026, 8, 1))][TOTAL]
        self.assertEqual((figure.covered, figure.days, figure.partial, figure.coverage_text),
                         (10, 31, True, "10 of 31 days"))

    def test_days_without_rows_inside_the_fetched_period_still_count_as_covered(self):  # G9
        full = make_session(D(2026, 8, 1), D(2026, 8, 31), Session.DAY, fetched=D(2026, 9, 2))
        for d in range(1, 32):
            if d not in (9, 16):  # two Sundays without a sale: Metabase returns no row for them
                put(full, 1, D(2026, 8, d), 10)
        figure = perfwide.month_figures()[(1, D(2026, 8, 1))][TOTAL]
        self.assertEqual((figure.value, figure.covered, figure.partial), (290, 31, False))
        early = make_session(D(2026, 9, 1), D(2026, 9, 30), Session.DAY, fetched=D(2026, 9, 12))
        put(early, 1, D(2026, 9, 3), 10)
        self.assertEqual(perfwide.month_figures()[(1, D(2026, 9, 1))][TOTAL].coverage_text, "12 of 30 days")

    def test_newest_session_wins_for_the_same_date(self):  # G44
        old = make_session(D(2026, 8, 1), D(2026, 8, 31), Session.DAY, fetched=D(2026, 9, 1))
        new = make_session(D(2026, 8, 1), D(2026, 8, 31), Session.DAY, fetched=D(2026, 9, 2))
        put(old, 1, D(2026, 8, 5), 10)
        put(new, 1, D(2026, 8, 5), 12)
        self.assertEqual(perfwide.month_values()[(1, D(2026, 8, 1))][TOTAL], 12)

    def test_infinite_values_are_left_out(self):  # G47
        s = make_session(D(2026, 8, 1), D(2026, 8, 31), Session.DAY)
        put(s, 1, D(2026, 8, 5), float("inf"))
        put(s, 1, D(2026, 8, 6), 5)
        self.assertEqual(perfwide.month_values()[(1, D(2026, 8, 1))][TOTAL], 5)

    def test_settings_lists_decide_what_is_a_rate(self):  # G46
        self.assertTrue(perfwide.is_rate("Average ticket"))
        lists = {"NOT_RATE": {"average ticket"}, "RATE_METRICS": {"members won"}}
        with mock.patch.object(perfwide, "_named", lambda setting: lists.get(setting, set())):
            self.assertFalse(perfwide.is_rate("Average Ticket"))
            self.assertTrue(perfwide.is_rate("Members won"))

    def test_a_full_rebuild_drops_months_whose_data_is_gone(self):  # G44 (stale drop)
        s = make_session(D(2026, 8, 1), D(2026, 8, 31))
        put(s, 1, D(2026, 8, 1), 100)
        perfwide.rebuild_months()
        self.assertEqual(CenterMonth.objects.get().covered_days, 31)
        s.delete()
        perfwide.rebuild_months()
        self.assertFalse(CenterMonth.objects.exists())

    def test_group_filter_and_month_parsing(self):  # G44
        CenterGroup.objects.all().delete()
        CenterGroup.objects.create(name="GYMS").centers.add(1)
        self.assertEqual(perfwide.center_ids_for("GYMS"), [1])
        self.assertEqual(sorted(perfwide.center_ids_for("Ungrouped")), [2, 3, 4])
        self.assertIsNone(perfwide.center_ids_for(""))
        self.assertEqual(perfwide.parse_month("2026-08"), D(2026, 8, 1))
        self.assertEqual(perfwide.parse_month("2026-08-19"), D(2026, 8, 1))
        self.assertEqual(perfwide.parse_month("0001-01", "x"), "x")
        self.assertEqual(perfwide.parse_month("nope"), None)

    def test_range_mode_is_capped_and_the_change_card_uses_gyms_with_both_figures(self):  # G44
        s = make_session(D(2026, 7, 1), D(2026, 8, 31))
        put(s, 1, D(2026, 7, 1), 100)
        put(s, 1, D(2026, 8, 1), 200)
        put(s, 2, D(2026, 8, 1), 999)  # no July figure: not part of the change
        mom = perfwide.analyse(TOTAL, mode="mom", month=D(2026, 8, 1), today=TODAY)
        self.assertEqual((mom["total_now"], mom["total_before"], mom["total_pct"]), (200, 100, 100.0))
        wide = perfwide.analyse(TOTAL, mode="range", a=(D(2026, 7, 1), D(2099, 1, 1)), b=(D(2026, 7, 1), D(2026, 7, 1)),
                                today=TODAY)
        self.assertEqual(next(r for r in wide["rows"] if r["id"] == 1)["now"], 300)


class RevenueMatrixTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        july = make_session(D(2026, 7, 1), D(2026, 7, 31), fetched=D(2026, 8, 2))
        august = make_session(D(2026, 8, 1), D(2026, 8, 31), fetched=D(2026, 8, 10))  # only 10 days of August
        september = make_session(D(2026, 9, 1), D(2026, 9, 30), fetched=D(2026, 10, 2))
        for cid, base in ((1, 9000), (2, 4000)):
            put(july, cid, D(2026, 7, 1), base)
            put(august, cid, D(2026, 8, 1), base / 3)
            put(september, cid, D(2026, 9, 1), base * 1.1)

    def check(self, data):
        r1 = next(r for r in data["rows"] if r["id"] == 1)
        self.assertEqual(r1["values"][D(2026, 8, 1)], 3000)  # still shown
        self.assertEqual(r1["partial"], {D(2026, 8, 1): "10 of 31 days"})
        self.assertEqual(r1["average"], (9000 + 9900) / 2)  # August is kept out
        self.assertIsNone(r1["mom_pct"])  # September vs a partial August means nothing
        ranks = next(s for s in data["sections"] if s["group"] == "Ungrouped")["rows"][0]["ranks"]
        self.assertEqual(ranks[data["rank_months"].index(D(2026, 8, 1))], None)
        self.assertIn("N of M days", data["partial_help"])

    def test_a_partly_fetched_month_is_flagged_and_kept_out_of_average_rank_and_change(self):  # G9
        self.check(revenue.matrix(months=4, today=TODAY))

    def test_the_stored_monthly_table_gives_the_same_answer(self):  # G28
        perfwide.rebuild_months()
        self.assertEqual(CenterMonth.objects.get(center_id=1, month=D(2026, 8, 1)).covered_days, 10)
        with mock.patch.object(perfwide, "month_figures", side_effect=AssertionError("raw rows read")):
            self.check(revenue.matrix(months=4, today=TODAY, stored=True))

    def test_a_range_outside_the_window_says_so_instead_of_showing_other_months(self):  # G51
        data = revenue.matrix(months=12, today=TODAY, since=D(2020, 1, 1), until=D(2020, 3, 1))
        self.assertFalse(data["has_data"])
        self.assertEqual(data["months"], [])
        self.assertIn("data starts Jul-26", data["notice"])
        self.assertIsNone(revenue.matrix(months=4, today=TODAY)["notice"])

    def test_equal_values_share_a_rank(self):  # G44
        self.assertEqual(revenue._ranks({1: 5, 2: 5, 3: 3, 4: None}), {1: 1, 2: 1, 3: 3})

    def test_group_table_reuses_a_matrix(self):  # G28
        data = revenue.matrix("", 12, TODAY)
        with mock.patch.object(revenue, "matrix", side_effect=AssertionError("computed twice")):
            table = revenue.group_month_table(today=TODAY, data=data)
        self.assertEqual(len(table["months"]), 12)


class MergeAndRepairTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.s = make_session(D(2026, 7, 1), D(2026, 7, 31), reports=("sales", "gym"), fetched=D(2026, 8, 1))

    def sale(self, uid, phone, name, revenue=100.0, session=None):
        return SalesRecord.objects.create(session=session or self.s, center_id=1, user_id=uid, phone=phone, name=name,
                                          pack_name="Gold", member_end_date=D(2026, 7, 20), revenue=revenue)

    def test_rows_without_a_user_id_are_not_merged_into_one_person(self):  # G10
        self.sale("", "98765 43210", "Asha", 100)
        self.sale("None", "+91 99999 11111", "Ravi", 200)
        self.sale("", "", "Meena", 300)
        self.sale("", "", "", 400)
        masters.merge_session(self.s)
        self.assertEqual(sorted(SalesPack.objects.values_list("revenue", flat=True)), [100, 200, 300, 400])
        later = make_session(D(2026, 7, 1), D(2026, 7, 31), fetched=D(2026, 8, 2))
        self.sale("", "9876543210", "Asha", 150, session=later)
        self.sale("", None, None, 400, session=later)  # the nameless row again
        masters.merge_session(later)
        self.assertEqual(SalesPack.objects.count(), 4)  # the same phone, and the same nameless row, on a later run
        self.assertEqual(SalesPack.objects.get(key__contains="~phone:9876543210").revenue, 150)

    def test_installments_of_one_pack_are_added_up(self):  # G44 (_combine)
        rows = [{"id": 1, "revenue": 300.0, "scraped_at": at(TODAY), "customer_id": None, "name": "A"},
                {"id": 2, "revenue": 700.0, "scraped_at": at(TODAY), "customer_id": 5, "name": "B"},
                {"id": 3, "revenue": None, "scraped_at": at(TODAY), "customer_id": None, "name": "C"}]
        row = masters._combine(rows, masters.SPECS["sales"])
        self.assertEqual((row["revenue"], row["name"], row["customer_id"]), (1000.0, "B", 5))

    def test_effective_end_never_passes_the_day_of_the_run(self):  # G44
        future = make_session(D(2026, 8, 1), D(2026, 12, 31), fetched=D(2026, 9, 3))
        self.assertEqual(customers.effective_end(future), D(2026, 9, 3))
        self.assertEqual(customers.effective_end(self.s), D(2026, 7, 31))

    def test_rebuild_all_resets_the_pack_so_old_wrong_data_goes(self):  # G27
        self.sale("U1", "9876543210", "Asha")
        customers.link_session(self.s)
        Customer.objects.filter(user_id="U1").update(pack_name="Bogus", pack_end_date=D(2030, 1, 1))
        customers.rebuild_all()
        c = Customer.objects.get(user_id="U1")
        self.assertEqual((c.pack_name, c.pack_end_date), ("Gold", D(2026, 7, 20)))

    def test_the_repair_runs_once_and_logs_what_it_did(self):  # G27
        self.sale("U1", "9876543210", "Asha")
        put(make_session(D(2026, 8, 1), D(2026, 8, 31)), 1, D(2026, 8, 1), 500)
        SalesPack.objects.create(key="1||Gold|2026-07-20", center_id=1, user_id="", revenue=999, last_session=self.s)
        entry = repairs.run_once()
        self.assertEqual(entry.error, "")
        self.assertEqual((entry.counts["customers"], entry.counts["months"], entry.counts["no_id_rows_dropped"]),
                         (1, 1, 1))
        self.assertEqual(CenterMonth.objects.count(), 1)
        self.assertIsNone(repairs.run_once())  # done already
        self.assertEqual(RepairLog.objects.count(), 1)

    def test_pruning_marks_the_session_and_checks_skip_it(self):  # G29
        GymMember.objects.create(session=self.s, center_id=1, user_id="U1")
        masters.merge_session(self.s)
        newer = make_session(D(2026, 7, 1), D(2026, 7, 31), reports=("sales",), fetched=D(2026, 8, 3))
        Session.objects.filter(pk__in=[self.s.pk, newer.pk]).update(status=Session.COMPLETE)
        self.assertEqual(checks.previous_like(Session.objects.get(pk=newer.pk)).pk, self.s.pk)
        masters.prune_run_rows(1)
        self.assertTrue(Session.objects.get(pk=self.s.pk).rows_pruned)
        self.assertFalse(Session.objects.get(pk=newer.pk).rows_pruned)
        self.assertIsNone(checks.previous_like(Session.objects.get(pk=newer.pk)))

    def test_deleting_a_session_keeps_its_day_coverage(self):  # G21
        SalesCoverage.objects.create(center_id=1, day=D(2026, 7, 5), session=self.s)
        self.s.delete()
        self.assertIsNone(SalesCoverage.objects.get().session)


class ChecksTests(FakeKronosMixin, TransactionTestCase):
    def run_like(self, rows, fetched, **kw):
        s = make_session(D(2026, 7, 1), D(2026, 7, 31), reports=("sales",), fetched=fetched, cids=(1, 2), **kw)
        for cid in (1, 2):
            SalesRecord.objects.bulk_create([SalesRecord(session=s, center_id=cid, user_id=f"U{i}") for i in range(rows)])
        Session.objects.filter(pk=s.pk).update(status=Session.COMPLETE)
        return Session.objects.get(pk=s.pk)

    def test_unlike_runs_are_not_compared(self):  # G49
        self.run_like(10, D(2026, 8, 1))
        gaps = self.run_like(40, D(2026, 8, 2), fill_missing=True, sales_chunk=Session.DAY)
        self.assertEqual(checks.run(gaps), [])  # a 'missing days only' run: no silent / changed findings
        by_day = self.run_like(40, D(2026, 8, 3), sales_chunk=Session.DAY)
        self.assertEqual(checks.run(by_day), [])  # asked by day, the earlier one whole: not comparable
        whole = self.run_like(40, D(2026, 8, 4))
        self.assertIn("changed a lot", checks.run(whole)[0]["text"])

    def test_post_processing_finding(self):  # G26
        finding = checks.post_processing_error("disk full")
        self.assertEqual((finding["level"], finding["kind"]), ("danger", "post_processing"))
        self.assertIn("disk full", finding["text"])


class SmallHelperTests(TransactionTestCase):
    def test_autorun_rejects_bad_campaigns(self):  # G17
        bad = AutoRun(name="x", reports=[], speed="warp", sales_chunk="year", granularity="hour")
        with self.assertRaises(ValidationError) as caught:
            bad.full_clean()
        self.assertTrue({"reports", "speed", "sales_chunk", "granularity"} <= set(caught.exception.message_dict))
        with self.assertRaises(ValidationError) as caught:
            AutoRun(name="y", reports=["sales", "nope"]).clean_fields()
        self.assertIn("nope", str(caught.exception))
        AutoRun(name="z", reports=["sales"]).full_clean()  # a good one passes
        AutoRun(name="w", reports=[]).clean_fields(exclude=["reports"])  # a form without the field: not its business

    def test_revenue_name_is_matched_exactly(self):  # G32
        for names, want in ((["Last Month Total Revenue", "Total Revenue"], "Total Revenue"),
                            (["Net Total Revenue", "1. Total Revenue", "Total Online Revenue"], "1. Total Revenue"),
                            (["1.a Total Revenue", "Total Revenue"], "1.a Total Revenue")):
            self.assertEqual(analysis.revenue_metrics(names)[0], want, names)
        self.assertEqual(analysis.revenue_metrics(["1.a New Revenue", "Previous New Revenue"])[1], "1.a New Revenue")
        # no exact name: a name that starts with the word beats one that only holds it
        self.assertEqual(analysis.revenue_metrics(["Last Month Total Revenue", "Total Revenue (INR)"])[0],
                         "Total Revenue (INR)")
        self.assertEqual(analysis.revenue_metrics(["Renewal Revenue", "2. New Revenue (INR)"])[1], "2. New Revenue (INR)")

    def test_safe_cells(self):  # G34
        self.assertEqual([numbers.safe_cell(v) for v in ("=HYPERLINK(1)", "@x", "+cmd", "-2+3", "\tx", "-354.5", "+91",
                                                         "Asha", 12, None, 1.5)],
                         ["'=HYPERLINK(1)", "'@x", "'+cmd", "'-2+3", "'\tx", "-354.5", "+91", "Asha", 12, None, 1.5])
        self.assertEqual(numbers.safe_cell("a\x00b\x1fc"), "abc")

    def test_non_finite_numbers(self):  # G47
        nan, inf = float("nan"), float("inf")
        self.assertEqual(numbers.total([1, nan, inf, None, 2]), 3)
        self.assertIsNone(numbers.total([nan]))
        self.assertEqual((numbers.tidy([nan, 1.5]), numbers.trim(inf)), ([None, 1.5], ""))
        line = sparkline.svg([1, nan, 3, inf], ["a", "b", "c", "d"])
        self.assertIn("<svg", line)
        self.assertNotIn("nan", line.lower())
        self.assertEqual(sparkline.svg([nan, 5]), "-")
        self.assertTrue(math.isnan(nan))
