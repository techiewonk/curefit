"""
Put the gyms of the management sheet into the groups GYMS / HYBRIDS / Gym Hybrid (by name).

    python manage.py seed_groups

Only groups that exist and are empty are filled, so it never undoes your own edits. It runs by itself after a
center sync and at start-up. For another sheet use `import_groups <file>`.
"""
from django.core.management.base import BaseCommand

from centers import services


class Command(BaseCommand):
    help = "Fill empty groups from the bundled list of gyms."

    def handle(self, *args, **opts):
        result = services.apply_group_seed()
        for name, n in result["groups"].items():
            self.stdout.write(f"{name}: {n} gyms added")
        for line in result["unmatched"]:
            self.stdout.write(f"  left alone - {line}")
        if not result["groups"]:
            self.stdout.write("Nothing to do: every group already has gyms (or no centers are synced yet).")
