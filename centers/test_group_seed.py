import json
import os
import tempfile

from django.core.management import call_command
from django.test import TransactionTestCase

from centers import services
from centers.models import Center, CenterGroup
from scraper.testing import FakeKronosMixin


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

    def test_the_command_and_the_migration(self):
        from importlib import import_module
        call_command("seed_groups")
        CenterGroup.objects.all().delete()
        migration = import_module("centers.migrations.0005_default_groups")
        from django.apps import apps
        migration.create_groups(apps, None)
        migration.create_groups(apps, None)  # repeating adds nothing
        self.assertEqual(list(CenterGroup.objects.values_list("name", flat=True)), ["GYMS", "HYBRIDS", "Gym Hybrid"])
