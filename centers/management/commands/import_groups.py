"""
Put centers into the groups GYMS / Gym Hybrid from a management sheet like 20oct.xls (tab-separated text or CSV).

    python manage.py import_groups 20oct.xls            # shows what would change
    python manage.py import_groups 20oct.xls --apply    # does it

The sheet has a header row whose first cell is the group name (GYMS, Gym Hybrid, ...), followed by one
gym per row. Gyms are matched to centers by name, ignoring case and punctuation. A name that matches
no center, or more than one, is listed and left alone.
"""
import csv
import re

from django.core.management.base import BaseCommand, CommandError

from centers.models import Center, CenterGroup

GROUPS = ("GYMS", "Gym Hybrid")


def norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


class Command(BaseCommand):
    help = "Set center groups from a management sheet."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--apply", action="store_true", help="save the groups (default: only show them)")

    def handle(self, *args, path, apply, **opts):
        try:
            with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
                text = f.read()
            rows = list(csv.reader(text.splitlines(), delimiter="\t" if "\t" in text.split("\n", 1)[0] else ","))
        except OSError as e:
            raise CommandError(str(e))
        names = {}
        for c in Center.objects.all():
            names.setdefault(norm(c.name), []).append(c)
        group, found, problems = None, {}, []
        for row in rows:
            cell = row[0].strip() if row else ""
            if cell in GROUPS:
                group = cell
            elif group and cell and cell.lower() != "month":
                matches = names.get(norm(cell), [])
                if len(matches) == 1:
                    found[matches[0].pk] = group
                else:
                    problems.append(f"{cell!r}: {'no center' if not matches else f'{len(matches)} centers'} match")
        for group_name in GROUPS:
            self.stdout.write(f"{group_name}: {sum(1 for g in found.values() if g == group_name)} gyms")
        for p in problems:
            self.stdout.write(f"  left alone - {p}")
        if apply:
            for position, group_name in enumerate(GROUPS):
                group, _ = CenterGroup.objects.get_or_create(name=group_name, defaults={"position": position})
                group.centers.add(*[k for k, g in found.items() if g == group_name])  # adds; never removes
            self.stdout.write("Saved. Add or remove gyms later under Center groups.")
        else:
            self.stdout.write("Nothing saved. Add --apply to save these groups.")
