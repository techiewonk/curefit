from django.db import migrations
from django.db.models import F
from django.db.models.functions import Substr

# Metabase gives "2026-06-11T00:00:00Z" or "2026-06-11": keep the day, so the columns can become
# real dates in the next migration (nullable ones: blank -> NULL).
FIELDS = {
    "salesrecord": ["member_end_date"],
    "perfmetric": ["metric_date"],
    "gymmember": ["purchase_date", "pack_start_date", "pack_end_date", "last_class_date"],
}


def keep_the_day(apps, schema_editor):
    for model, fields in FIELDS.items():
        Model = apps.get_model("scraper", model)
        for field in fields:
            if Model._meta.get_field(field).null:
                Model.objects.filter(**{field: ""}).update(**{field: None})
            Model.objects.exclude(**{f"{field}__isnull": True}).update(**{field: Substr(F(field), 1, 10)})


class Migration(migrations.Migration):
    dependencies = [("scraper", "0005_session_checks")]
    operations = [migrations.RunPython(keep_the_day, migrations.RunPython.noop)]
