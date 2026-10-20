from django.db import migrations


def create_lists(apps, schema_editor):
    CenterList = apps.get_model("centers", "CenterList")
    CenterList.objects.get_or_create(name="actual", defaults={"description": "The real run"})
    CenterList.objects.get_or_create(name="test", defaults={"description": "A few centers for trial runs"})


class Migration(migrations.Migration):
    dependencies = [("centers", "0001_initial")]
    operations = [migrations.RunPython(create_lists, migrations.RunPython.noop)]
