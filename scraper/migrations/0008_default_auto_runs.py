import datetime

from django.db import migrations

CORE = ["sales", "perf", "gym"]

# name, center list, frequency, granularity, time of day, weekday, day of month, speed
DEFAULTS = [
    ("Daily report (actual list)", "actual", "daily", "day", datetime.time(6, 30), 0, 1, "adaptive"),
    ("Weekly report (actual list)", "actual", "weekly", "week", datetime.time(6, 40), 0, 1, "adaptive"),
    ("Monthly report (actual list)", "actual", "monthly", "month", datetime.time(6, 50), 0, 2, "adaptive"),
    ("Daily test (test list)", "test", "daily", "day", datetime.time(6, 35), 0, 1, "gentle"),
]


def create_defaults(apps, schema_editor):
    """Switched-off automatic runs to enable, and a test one to try with 'Run now'."""
    AutoRun = apps.get_model("scraper", "AutoRun")
    CenterList = apps.get_model("centers", "CenterList")
    for name, list_name, frequency, granularity, run_at, weekday, month_day, speed in DEFAULTS:
        centers = CenterList.objects.filter(name=list_name).first()
        if centers:
            AutoRun.objects.get_or_create(name=name, defaults={
                "centers": centers, "frequency": frequency, "granularity": granularity, "run_at": run_at,
                "weekday": weekday, "month_day": month_day, "reports": CORE, "speed": speed, "days": 1,
                "enabled": False})


class Migration(migrations.Migration):
    dependencies = [("scraper", "0007_dates_cancelled_granularity_autorun"), ("centers", "0002_default_lists")]
    operations = [migrations.RunPython(create_defaults, migrations.RunPython.noop)]
