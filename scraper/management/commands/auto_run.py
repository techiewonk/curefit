"""
Start an automatic run from the command line, for Windows Task Scheduler or cron.

    python manage.py auto_run --list
    python manage.py auto_run "Daily report (actual list)"          # queue it for the job runner
    python manage.py auto_run "Daily report (actual list)" --wait   # do the whole scrape in this process

--wait needs no job runner and returns when the scrape is done (exit code 0 = complete, 1 = something
is missing or it failed, 2 = skipped, for instance not logged in). Without --wait it only queues the
run, so `manage.py serve` (the job runner) has to be running.

The run reports on the period its own settings give (yesterday, last week, last month, ...).

Examples
    Windows (every day at 06:30):
      schtasks /Create /SC DAILY /ST 06:30 /TN "Curefit daily" /TR "\"D:\\path\\.venv\\Scripts\\python.exe\" \"D:\\path\\manage.py\" auto_run \"Daily report (actual list)\" --wait"
    Linux / macOS crontab:
      30 6 * * * cd /path/to/project && .venv/bin/python manage.py auto_run "Daily report (actual list)" --wait
"""
from django.core.management.base import BaseCommand, CommandError

from scraper import autorun
from scraper.models import AutoRun


class Command(BaseCommand):
    help = "Start an automatic run (for Task Scheduler / cron)."

    def add_arguments(self, parser):
        parser.add_argument("name", nargs="?", help="name of the automatic run (see --list)")
        parser.add_argument("--list", action="store_true", help="show the automatic runs and exit")
        parser.add_argument("--wait", action="store_true", help="do the scrape in this process and wait for it")
        parser.add_argument("--force", action="store_true",
                            help="run even if it is switched off or already started today")

    def handle(self, *args, **opts):
        if opts["list"] or not opts["name"]:
            for a in AutoRun.objects.select_related("centers"):
                start, end = autorun.period(a)
                when = a.cron if a.frequency == a.CRON else f"{a.frequency} at {a.run_at:%H:%M}"
                self.stdout.write(f"{'on ' if a.enabled else 'off'}  {a.name}  [{when}]  list {a.centers.name}  "
                                  f"period {start} to {end} by {a.granularity}")
            return
        try:
            auto = AutoRun.objects.get(name=opts["name"])
        except AutoRun.DoesNotExist:
            raise CommandError(f"No automatic run named {opts['name']!r}. Use --list.")
        status, message = autorun.run(auto, force=opts["force"], inline=opts["wait"])
        self.stdout.write(f"{status}: {message}")
        raise SystemExit({"started": 0, "finished": 0, "skipped": 2}.get(status, 1))
