from django.db import migrations


def legacy_to_complete(apps, schema_editor):
    """Sessions imported from the old scripts were finished scrapes: they are ordinary sessions now."""
    apps.get_model("scraper", "Session").objects.filter(status="legacy").update(status="complete")


class Migration(migrations.Migration):
    dependencies = [("scraper", "0002_remove_appsetting")]
    operations = [migrations.RunPython(legacy_to_complete, migrations.RunPython.noop)]
