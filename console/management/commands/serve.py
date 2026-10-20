"""
Start the admin: the web server and the job runner in one command.

    python manage.py serve                 # waitress on http://localhost:8000
    python manage.py serve --reload        # development: the web server restarts on code changes
    python manage.py serve --port 9000 --no-open

The job runner (django-q2's qcluster) is its own process, so a running scrape keeps going when the
web server restarts. Stopping this command stops both; a scrape that was running shows up as
"interrupted" and can be continued.
"""
import os
import subprocess
import sys
import threading
import webbrowser

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.core.wsgi import get_wsgi_application


def stop(process):
    """End the runner and the worker processes it started."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
    else:
        process.terminate()
    process.wait(timeout=15)


class Command(BaseCommand):
    help = "Run the web server and the job runner together."

    def add_arguments(self, parser):
        parser.add_argument("--port", type=int, default=8000)
        parser.add_argument("--reload", action="store_true", help="development: restart the web server on code changes")
        parser.add_argument("--no-jobs", action="store_true", help="web server only (no job runner)")
        parser.add_argument("--no-open", action="store_true", help="do not open the browser")

    def handle(self, *args, **opts):
        port, url = opts["port"], f"http://localhost:{opts['port']}/"
        # A fresh start migrates and runs the start-up steps. With --reload Django restarts this command in a child
        # process (RUN_MAIN) on every code change: those restarts do not repeat them.
        if os.environ.get("RUN_MAIN") != "true":
            call_command("migrate", interactive=False, verbosity=0)  # a new version never meets an old database
            call_command("seed_groups", verbosity=0)
            call_command("link_customers")  # fills in customers for sessions scraped before they existed
            call_command("merge_masters")
            call_command("merge_months")
        runner = None
        # With --reload Django re-runs this command in a child process: only the outer one starts the runner.
        if not opts["no_jobs"] and os.environ.get("RUN_MAIN") != "true":
            runner = subprocess.Popen([sys.executable, str(settings.BASE_DIR / "manage.py"), "qcluster"],
                                      cwd=settings.BASE_DIR)
        if not opts["no_open"] and os.environ.get("RUN_MAIN") != "true":
            threading.Timer(2.0, webbrowser.open, [url]).start()
        self.stdout.write(f"Curefit admin: {url}" + (" (auto-reload on)" if opts["reload"] else ""))
        try:
            if opts["reload"]:
                call_command("runserver", f"127.0.0.1:{port}")
            else:
                from waitress import serve
                serve(get_wsgi_application(), host="127.0.0.1", port=port, threads=8)
        finally:
            if runner:
                stop(runner)
