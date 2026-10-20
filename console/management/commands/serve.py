"""
Start the admin: the web server and the job runner in one command.

    python manage.py serve                 # waitress on http://localhost:8000
    python manage.py serve --reload        # development: the web server restarts on code changes
    python manage.py serve --port 9000 --no-open

The job runner (django-q2's qcluster) is its own process, so a running scrape keeps going when the
web server restarts. Stopping this command stops both; a scrape that was running shows up as
"interrupted" and can be continued. Closing the window stops the runner too (Windows ties it to this
process), and a runner left over from a crashed start is ended at the next start (runner.pid).

Start-up order: migrations, then the job runner and the web server, then (in the background) the
catch-up steps that fill the merged tables from older sessions. A failing catch-up step is reported and
never stops the start.
"""
import os
import subprocess
import sys
import threading
import time
import webbrowser

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.core.wsgi import get_wsgi_application
from django.db import connection

RUNNER_PID = settings.BASE_DIR / "runner.pid"


def _repair_once():
    """The one-time data repair (scraper/repairs.py): does nothing once its version has run without an error."""
    from scraper import repairs
    entry = repairs.run_once()
    if entry is not None and entry.error:
        raise RuntimeError(entry.error)


# Catch-up steps run in the background after start (each one on its own; they are safe to repeat).
# A name is a management command; a (name, function) pair is called directly. The one-time repair goes first: it
# rebuilds what the others would otherwise catch up on with the old results.
STARTUP_STEPS = (("one-time data repair", _repair_once), "link_customers", "merge_masters", "merge_months")
WATCH_EVERY = 5  # seconds between two looks at the job runner
_jobs = []  # Windows job objects: kept open for the life of this process (closing one ends its processes)


def _kill_tree(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def stop(process):
    """End the runner and the worker processes it started. Never raises because it took too long."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        _kill_tree(process.pid)
    else:
        process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _looks_like_python(pid):
    """A pid from runner.pid may have been reused by another program after a reboot: only end a Python."""
    if os.name != "nt":
        return True
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True)
    return "python" in (out.stdout or "").lower()


def end_stale_runner(write=print):
    """A job runner left running by an earlier start (window closed, crash) would scrape twice: end it."""
    from scraper.jobs import pid_alive
    try:
        pid = int(RUNNER_PID.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid != os.getpid() and pid_alive(pid) and _looks_like_python(pid):
        write(f"Ending the job runner left over from the last start (pid {pid}).")
        _kill_tree(pid)
    else:
        pid = None
    try:
        RUNNER_PID.unlink()
    except OSError:
        pass
    return pid


def tie_to_this_process(process):
    """Windows: put the runner in a job object that ends it (and its workers) when this process ends, even when
    the window is simply closed. Best effort: returns the job handle, or None."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        if not kernel32.AssignProcessToJobObject(job, int(process._handle)):
            return None
        _jobs.append(job)
        return job
    except Exception:  # noqa: BLE001 - without it the runner pid file still catches a left-over runner
        return None


def run_startup_steps(write=print, steps=STARTUP_STEPS):
    """The catch-up steps, each on its own: one that fails is reported and the others still run."""
    failed = []
    try:
        for step in steps:
            name, fn = step if isinstance(step, tuple) else (step, None)
            try:
                fn() if fn else call_command(name)
            except Exception as e:  # noqa: BLE001 - the admin must start whatever the data looks like
                failed.append(name)
                later = "it is tried again at the next start" if fn else f"run 'python manage.py {name}' later to finish it"
                write(f"Start-up step '{name}' failed ({e.__class__.__name__}: {str(e)[:300]}). "
                      f"The admin works anyway; {later}.")
    finally:
        connection.close()  # this thread's own database connection
    return failed


def watch_runner(runner, write=print, every=WATCH_EVERY):
    """Say so when the job runner dies: scrapes and automatic runs would otherwise just never start."""
    while True:
        code = runner.poll()
        if code is not None:
            write(f"WARNING: the job runner stopped (exit code {code}). Scrapes and automatic runs will not start "
                  "until you restart run.bat.")
            return code
        time.sleep(every)


def checkpoint():
    """Fold the SQLite write-ahead log back into db.sqlite3, so the database file alone holds everything."""
    try:
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:  # noqa: BLE001 - shutting down anyway
        pass


class Command(BaseCommand):
    help = "Run the web server and the job runner together (the job runner does the scraping)."

    def add_arguments(self, parser):
        parser.add_argument("--port", type=int, default=8000, help="web server port (default 8000)")
        parser.add_argument("--reload", action="store_true", help="development: restart the web server on code changes")
        parser.add_argument("--no-jobs", action="store_true", help="web server only (no job runner: scrapes wait)")
        parser.add_argument("--no-open", action="store_true", help="do not open the browser")

    def handle(self, *args, **opts):
        port, url = opts["port"], f"http://localhost:{opts['port']}/"
        write = self.stdout.write
        # A fresh start migrates and runs the start-up steps. With --reload Django restarts this command in a child
        # process (RUN_MAIN) on every code change: those restarts do not repeat them.
        fresh = os.environ.get("RUN_MAIN") != "true"
        runner = None
        if fresh:
            call_command("migrate", interactive=False, verbosity=0)  # a new version never meets an old database
            try:
                call_command("seed_groups", verbosity=0)
            except Exception as e:  # noqa: BLE001
                write(f"Start-up step 'seed_groups' failed ({e}); groups can be filled later.")
            if not opts["no_jobs"]:
                end_stale_runner(write)
                runner = subprocess.Popen([sys.executable, str(settings.BASE_DIR / "manage.py"), "qcluster"],
                                          cwd=settings.BASE_DIR)
                try:
                    RUNNER_PID.write_text(str(runner.pid))
                except OSError:
                    pass
                tie_to_this_process(runner)
                threading.Thread(target=watch_runner, args=(runner, write), daemon=True).start()
            threading.Thread(target=run_startup_steps, args=(write,), daemon=True).start()
            if not opts["no_open"]:
                threading.Timer(2.0, webbrowser.open, [url]).start()
        write(f"Curefit admin: {url}" + (" (auto-reload on)" if opts["reload"] else ""))
        try:
            if opts["reload"]:
                call_command("runserver", f"127.0.0.1:{port}")
            else:
                from waitress import serve
                serve(get_wsgi_application(), host="127.0.0.1", port=port, threads=8)
        finally:
            if runner:
                stop(runner)
                try:
                    RUNNER_PID.unlink()
                except OSError:
                    pass
            if fresh:
                checkpoint()
