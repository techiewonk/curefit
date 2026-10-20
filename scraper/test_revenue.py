import datetime as dt

from django.test import TransactionTestCase

from centers.models import Center, CenterGroup
from scraper import engine, revenue
from scraper.models import PerfMetric
from scraper.testing import FakeKronosMixin

D = dt.date
TOTAL = "1. Total Revenue"


class RevenueTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.a = engine.new_session("2026-06-01", "2026-08-31", [1, 2], ["perf"])
        self.b = engine.new_session("2026-08-01", "2026-09-30", [1, 2], ["perf"])
        Center.objects.filter(pk=1).update(monthly_target=2000)
        CenterGroup.objects.create(name="GYMS").centers.add(1)
        CenterGroup.objects.create(name="Gym Hybrid").centers.add(2)

    def put(self, session, cid, month, value):
        PerfMetric.objects.create(session=session, center_id=cid, metric_date=month, metric=TOTAL, value=value)

    def test_matrix(self):
        today = D(2026, 10, 15)
        for cid, base in ((1, 1000), (2, 500)):
            self.put(self.a, cid, D(2026, 7, 1), base)
            self.put(self.a, cid, D(2026, 8, 1), base * 2)
        self.put(self.b, 1, D(2026, 8, 1), 3000)   # a newer session corrects August for center 1
        self.put(self.b, 1, D(2026, 9, 1), 2400)
        self.put(self.b, 2, D(2026, 9, 1), 500)
        self.put(self.b, 1, D(2026, 10, 1), 100)   # the current month is partial
        data = revenue.matrix(months=4, today=today)
        r1 = next(r for r in data["rows"] if r["id"] == 1)
        r2 = next(r for r in data["rows"] if r["id"] == 2)
        self.assertEqual(r1["values"][D(2026, 8, 1)], 3000)
        self.assertEqual(data["latest_month"], D(2026, 9, 1))
        self.assertEqual(r1["average"], (1000 + 3000 + 2400) / 3)  # October (partial) is left out
        self.assertEqual((r1["rank_latest"], r2["rank_latest"], r1["rank_average"]), (1, 2, 1))
        self.assertEqual(r1["mom_pct"], -20.0)
        self.assertEqual(r1["achievement"], 120.0)
        self.assertEqual(set(data["group_totals"]), {"GYMS", "Gym Hybrid"})
        summary = {g["group"]: g for g in data["summary"]}
        self.assertEqual((summary["GYMS"]["latest"], summary["GYMS"]["achievement"], summary["GYMS"]["best"]["id"]),
                         (2400, 120.0, 1))
        self.assertEqual(data["summary"][0]["group"], "GYMS")  # biggest first
        only = revenue.matrix(group="Gym Hybrid", months=4, today=today)
        self.assertEqual([r["id"] for r in only["rows"]], [2])
        CenterGroup.objects.get(name="Gym Hybrid").centers.add(1)  # in two groups: counts in both
        both = revenue.matrix(months=4, today=today)
        self.assertEqual(len(both["group_totals"]), 2)
        self.assertEqual(next(r for r in both["rows"] if r["id"] == 1)["group"], "GYMS, Gym Hybrid")
        CenterGroup.objects.get(name="Gym Hybrid").centers.remove(1, 2)
        self.assertEqual([r["id"] for r in revenue.matrix(group="Ungrouped", months=4, today=today)["rows"]], [2])

    def test_page_and_download(self):
        self.put(self.a, 1, D(2026, 8, 1), 1000)
        self.put(self.a, 1, D(2026, 9, 1), 1500)
        r = self.client.get("/admin/console/revenue/", {"group": "GYMS"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/admin/console/revenue/", {"months": "x"}).status_code, 200)
        csv = self.client.get("/admin/console/revenue/csv/")
        self.assertTrue(csv.content.decode().startswith("center_id,gym,group,"))
        self.assertIn("spreadsheetml", self.client.get("/admin/console/revenue/csv/", {"format": "xlsx"})["Content-Type"])

    def test_empty_page_explains_what_to_run(self):
        self.assertContains(self.client.get("/admin/console/revenue/"), "Split by: month")

    def test_center_admin_groups(self):
        from django.urls import reverse
        url = reverse("admin:centers_center_changelist")
        self.assertEqual(self.client.get(url + "?groups__name=GYMS").status_code, 200)
        group = CenterGroup.objects.get(name="Gym Hybrid")
        r = self.client.post(url, {"action": f"add_group_{group.pk}", "_selected_action": [1, 2]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(set(group.centers.values_list("pk", flat=True)), {1, 2})
        self.client.post(url, {"action": f"remove_group_{group.pk}", "_selected_action": [1]})
        self.assertEqual(set(group.centers.values_list("pk", flat=True)), {2})
        self.assertEqual(self.client.get(reverse("admin:centers_centergroup_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:centers_centergroup_change", args=[group.pk])).status_code, 200)

    def test_import_groups_from_a_sheet(self):
        import os
        import tempfile
        from django.core.management import call_command
        CenterGroup.objects.all().delete()
        Center.objects.filter(pk=1).update(name="Cult Gym HSR")
        Center.objects.filter(pk=2).update(name="Cult Gym Yemlur")
        sheet = "GYMS\tJun-24\nCult Gym HSR\t1\n\nGym Hybrid\tx\ncult gym  yemlur\t2\nCult Gym Nowhere\t3\n"
        with tempfile.NamedTemporaryFile("w", suffix=".xls", delete=False, encoding="utf-8") as f:
            f.write(sheet)
        self.addCleanup(os.remove, f.name)
        call_command("import_groups", f.name)
        self.assertFalse(CenterGroup.objects.exists())  # nothing saved without --apply
        call_command("import_groups", f.name, "--apply")
        self.assertEqual(list(CenterGroup.objects.get(name="GYMS").centers.values_list("pk", flat=True)), [1])
        self.assertEqual(list(CenterGroup.objects.get(name="Gym Hybrid").centers.values_list("pk", flat=True)), [2])
