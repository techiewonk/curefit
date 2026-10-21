import json
import os
import tempfile

from django.core.management import call_command
from django.test import TransactionTestCase

from centers import services
from centers.models import Center, CenterGroup
from scraper.testing import FakeKronosMixin


class CenterSyncTests(FakeKronosMixin, TransactionTestCase):
    """Centers 1..4 exist (ACTIVE, named Cult 1..4)."""

    def test_bad_records_are_skipped_blanks_never_wipe_and_missing_centers_are_marked_removed(self):
        diff = services.upsert_from_kronos([
            {"id": 1, "state": "ACTIVE"},                 # no name in this answer
            {"id": "2", "name": "Cult 2 new", "state": ""},
            {"id": 2, "name": "duplicate", "state": "INACTIVE"},  # the same id again: counts once
            {"name": "no id"}, {"id": "abc"}, {"id": None},
            {"id": 9, "name": "New gym", "state": "ACTIVE"}])
        self.assertEqual(Center.objects.get(pk=1).name, "Cult 1")
        self.assertEqual((Center.objects.get(pk=2).name, Center.objects.get(pk=2).state), ("Cult 2 new", "ACTIVE"))
        self.assertEqual(diff["total"], 3)
        self.assertEqual({r["id"] for r in diff["removed"]}, {3, 4})
        self.assertEqual(set(Center.objects.filter(state=Center.REMOVED).values_list("pk", flat=True)), {3, 4})
        again = services.upsert_from_kronos([{"id": 1, "state": "ACTIVE"}, {"id": 2}, {"id": 9}, {"id": 3, "state": "ACTIVE"}])
        self.assertEqual(again["removed"], [])  # 4 was already marked
        self.assertEqual(Center.objects.get(pk=3).state, "ACTIVE")  # back in Kronos: active again
        services.upsert_from_kronos([{"id": 1}, {"id": 2}, {"id": 9}, {"id": 3}, {"id": 4}])  # 4 back, state unknown
        self.assertEqual(Center.objects.get(pk=4).state, "")
        self.assertEqual(Center.objects.get(pk=3).state, "ACTIVE")  # a known state is still never blanked

    def test_an_empty_or_tiny_answer_changes_nothing(self):
        from unittest import mock
        with self.assertRaises(ValueError):
            services.upsert_from_kronos([{"name": "no id"}])
        Center.objects.bulk_create([Center(center_id=100 + i, name=f"G{i}", state="ACTIVE") for i in range(30)])
        with mock.patch.object(services, "CenterSyncLog") as logged, self.assertRaises(ValueError):
            services.upsert_from_kronos([{"id": 1, "name": "x", "state": "ACTIVE"}])
        logged.objects.create.assert_not_called()
        self.assertFalse(Center.objects.filter(state=Center.REMOVED).exists())
        self.assertEqual(Center.objects.get(pk=1).name, "Cult 1")


class GroupSeedTests(FakeKronosMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        for pk, name in ((1, "Cult Gym HSR"), (2, "Cult Gym Marathahalli"), (3, "Cult Gym Indiranagar"),
                         (4, "Cult Gym Indiranagar"), (5, "Cult Gym Yemlur")):
            Center.objects.update_or_create(pk=pk, defaults={"name": name})
        for position, name in enumerate(("GYMS", "HYBRIDS", "Gym Hybrid")):
            CenterGroup.objects.get_or_create(name=name, defaults={"position": position})[0].centers.clear()
        self.seed = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"GYMS": ["cult gym  hsr", "Cult Gym Indiranagar", "Nowhere"], "HYBRIDS": ["Cult Gym Marathahalli"],
                   "Gym Hybrid": ["Cult Gym Yemlur"]}, self.seed)
        self.seed.close()
        self.addCleanup(os.remove, self.seed.name)

    def ids(self, name):
        return set(CenterGroup.objects.get(name=name).centers.values_list("pk", flat=True))

    def test_gyms_are_matched_by_name_and_ambiguous_or_unknown_names_are_reported(self):
        result = services.apply_group_seed(self.seed.name)
        self.assertEqual((self.ids("GYMS"), self.ids("HYBRIDS"), self.ids("Gym Hybrid")), ({1}, {2}, {5}))
        self.assertEqual(result["added"], 3)
        self.assertEqual(len(result["unmatched"]), 2)  # Indiranagar matches two centers, Nowhere none

    def test_it_never_undoes_your_edits(self):
        services.apply_group_seed(self.seed.name)
        CenterGroup.objects.get(name="GYMS").centers.add(3)          # you added one
        CenterGroup.objects.get(name="HYBRIDS").centers.remove(2)    # you took one out ... of a group that is now empty
        CenterGroup.objects.get(name="Gym Hybrid").delete()         # and deleted a group
        result = services.apply_group_seed(self.seed.name)
        self.assertEqual(self.ids("GYMS"), {1, 3})
        self.assertEqual(self.ids("HYBRIDS"), {2})  # an emptied group is refilled: a group with no gyms has nothing to protect
        self.assertFalse(CenterGroup.objects.filter(name="Gym Hybrid").exists())  # never recreated here
        self.assertEqual(result["groups"], {"HYBRIDS": 1})

    def test_the_bundled_list_has_the_three_groups_of_the_sheet(self):
        seed = json.loads(services.GROUP_SEED.read_text(encoding="utf-8"))
        self.assertEqual({k: len(v) for k, v in seed.items()}, {"GYMS": 8, "HYBRIDS": 12, "Gym Hybrid": 14})

    def test_ungrouped_is_a_reserved_group_name(self):
        from django.core.exceptions import ValidationError
        for name in ("Ungrouped", " ungrouped "):
            with self.assertRaises(ValidationError):
                CenterGroup(name=name).full_clean()
        CenterGroup(name="Ungrouped gyms").full_clean()

    def test_the_command_and_the_migration(self):
        from importlib import import_module
        call_command("seed_groups")
        CenterGroup.objects.all().delete()
        migration = import_module("centers.migrations.0005_default_groups")
        from django.apps import apps
        migration.create_groups(apps, None)
        migration.create_groups(apps, None)  # repeating adds nothing
        self.assertEqual(list(CenterGroup.objects.values_list("name", flat=True)), ["GYMS", "HYBRIDS", "Gym Hybrid"])

    def test_import_groups_skips_a_section_named_ungrouped(self):
        from io import StringIO
        sheet = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        sheet.write("GYMS,Jan\nCult Gym HSR,1\nUngrouped,Jan\nCult Gym Yemlur,2\n")
        sheet.close()
        self.addCleanup(os.remove, sheet.name)
        out = StringIO()
        call_command("import_groups", sheet.name, "--apply", stdout=out)
        self.assertFalse(CenterGroup.objects.filter(name__iexact="ungrouped").exists())
        self.assertEqual(self.ids("GYMS"), {1})
        self.assertIn("reserved", out.getvalue())
