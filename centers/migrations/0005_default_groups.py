from django.db import migrations

GROUPS = ["GYMS", "HYBRIDS", "Gym Hybrid"]  # the groups of the management sheet, in its order


def create_groups(apps, schema_editor):
    CenterGroup = apps.get_model("centers", "CenterGroup")
    for position, name in enumerate(GROUPS):
        CenterGroup.objects.get_or_create(name=name, defaults={"position": position})


class Migration(migrations.Migration):
    dependencies = [("centers", "0004_center_note")]
    operations = [migrations.RunPython(create_groups, migrations.RunPython.noop)]
