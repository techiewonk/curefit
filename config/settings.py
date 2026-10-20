"""
Settings for the Curefit admin (local, single user, SQLite).

Environment:
    CUREFIT_DEBUG=1        development (dev.bat sets it): runserver auto-reload, DEBUG pages
    CUREFIT_DB=<path>      use another SQLite file (tests, experiments)
"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.environ.get("CUREFIT_DEBUG", "0") == "1"


def _secret_key():
    """Per-install secret kept next to the project (git-ignored), never hard-coded."""
    path = BASE_DIR / ".django_secret"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(50), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


SECRET_KEY = _secret_key()
# The admin has no password page (only the Kronos OTP): it must never be reachable from elsewhere.
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
CSRF_TRUSTED_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"]

INSTALLED_APPS = [
    "unfold",                        # modern admin theme (must precede django.contrib.admin)
    "unfold.contrib.filters",
    "unfold.contrib.import_export",
    "unfold.contrib.constance",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "import_export",
    "constance",
    "constance.backends.database",
    "django_q",
    "centers",
    "scraper",
    "console",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "config.middleware.LocalAutoLogin",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],  # project templates win over the apps' (Home dashboard)
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("CUREFIT_DB") or BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            # several centers are written at once, plus the job runner: take the write lock up
            # front and wait for it instead of failing with "database is locked"
            "transaction_mode": "IMMEDIATE",
            "timeout": 60,
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;",
        },
        # tests use a real file (not :memory:) so the scraper's worker threads share one database
        "TEST": {"NAME": BASE_DIR / "test_db.sqlite3"},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# Background jobs: the Django database is the queue (no Redis/broker). One job at a time; the
# scrape itself fans out over centers inside the task. `manage.py qcluster` runs it.
Q_CLUSTER = {
    "name": "curefit",
    "workers": 1,
    "orm": "default",
    "timeout": 60 * 60 * 24 * 6,  # a full scrape can take many hours
    "retry": 60 * 60 * 24 * 7,    # must exceed the timeout, or q2 would queue the task again
    "max_attempts": 1,
    "poll": 1,
    "queue_limit": 20,
    "save_limit": 250,
    "catch_up": True,  # a missed automatic run fires when the runner starts again (once a day at most)
}

from django.templatetags.static import static  # noqa: E402
from django.urls import reverse_lazy  # noqa: E402

_ADMIN = lambda model: reverse_lazy(f"admin:{model}_changelist")  # noqa: E731

# Settings editable in the admin (System → Settings). Read them with `constance.config.NAME`.
IMPORT_EXPORT_ESCAPE_FORMULAE_ON_EXPORT = True  # a name like =HYPERLINK(...) must not run when opened in Excel

CONSTANCE_BACKEND = "constance.backends.database.DatabaseBackend"
CONSTANCE_CONFIG = {
    "DELAY_MIN": (1.0, "Shortest pause between requests (seconds)", float),
    "DELAY_MAX": (3.0, "Longest pause between requests (seconds)", float),
    "MAX_RETRIES": (4, "Tries for a failed request", int),
    "BACKOFF_BASE": (2.0, "Wait before the first retry (seconds); doubles each time", float),
    "POLL_SECONDS": (240, "How long to wait for a slow Metabase query (seconds)", int),
    "REQUEST_TIMEOUT": (300, "Network timeout per request (seconds)", int),
    "ROW_CAP": (10000, "A result this long may have been cut off by Metabase", int),
    "AUTH_FAIL_LIMIT": (3, "Consecutive 401/403 answers before a run stops", int),
    "REPORT_FAIL_LIMIT": (3, "Failures in a row (none succeeded) before a report is skipped", int),
    "MAX_WORKERS": (4, "Most centers fetched at the same time", int),
    "LOGIN_EMAIL": ("shaurya@teamfit.in", "Kronos account that receives the login OTP", str),
    "KEEP_RUN_ROWS": (0, "Keep the per-run sales and gym rows of only this many newest sessions (0 = all). "
                         "The all-runs tables keep the data either way", int),
    "SALES_CHUNK": ("whole", "How Expired Pack User Details is asked for by default: whole, day or week", str),
    "LEAD_EXPIRING_DAYS": (30, "A pack ending within this many days makes the customer 'expiring soon'", int),
    "LEAD_EXPIRED_DAYS": (90, "A pack that ended within this many days is 'expired recently'; older is 'lapsed'", int),
}
CONSTANCE_CONFIG_FIELDSETS = {
    "Requests": ("DELAY_MIN", "DELAY_MAX", "MAX_RETRIES", "BACKOFF_BASE", "POLL_SECONDS", "REQUEST_TIMEOUT"),
    "Safety": ("ROW_CAP", "AUTH_FAIL_LIMIT", "REPORT_FAIL_LIMIT", "MAX_WORKERS"),
    "Account": ("LOGIN_EMAIL",),
    "Leads": ("LEAD_EXPIRING_DAYS", "LEAD_EXPIRED_DAYS"),
    "Sales report": ("SALES_CHUNK",),
    "Storage": ("KEEP_RUN_ROWS",),
}

UNFOLD = {
    "SITE_TITLE": "Curefit Admin",
    "SITE_HEADER": "Curefit Admin",
    "SITE_SYMBOL": "monitoring",
    "SHOW_HISTORY": False,
    "STYLES": [lambda request: static("console/wide.css")],
    "SCRIPTS": [lambda request: static("console/datatable.js")],
    "SITE_FAVICONS": [{"rel": "icon", "sizes": "32x32", "type": "image/svg+xml",
                       "href": lambda request: static("console/favicon.svg")}],
    "DASHBOARD_CALLBACK": "console.dashboard.callback",
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": [
            {"title": "Performance", "separator": False, "items": [
                {"title": "Dashboard", "icon": "dashboard", "link": reverse_lazy("admin:index")},
                {"title": "Revenue by group", "icon": "leaderboard", "link": reverse_lazy("console:revenue")},
                {"title": "Monthly metrics", "icon": "query_stats", "link": _ADMIN("scraper_centermonth")},
                {"title": "Analysis and compare", "icon": "insights", "link": reverse_lazy("console:perf_analysis")},
                {"title": "Other cards", "icon": "dataset", "link": _ADMIN("scraper_cardrow")},
            ]},
            {"title": "Gym members", "separator": True, "items": [
                {"title": "All", "icon": "groups", "link": _ADMIN("scraper_gympack")},
                {"title": "Per run / session", "icon": "history", "link": _ADMIN("scraper_gymmember")},
                {"title": "Leads", "icon": "person_search", "link": _ADMIN("scraper_customer")},
            ]},
            {"title": "Expired packs (sales)", "separator": True, "items": [
                {"title": "All", "icon": "receipt_long", "link": _ADMIN("scraper_salespack")},
                {"title": "Per run / session", "icon": "history", "link": _ADMIN("scraper_salesrecord")},
                {"title": "Day coverage", "icon": "event_available", "link": _ADMIN("scraper_salescoverage")},
                {"title": "Missing days", "icon": "event_busy", "link": reverse_lazy("console:coverage")},
            ]},
            {"title": "Operate", "separator": True, "items": [
                {"title": "Status", "icon": "monitor_heart", "link": reverse_lazy("console:status")},
                {"title": "Run a scrape", "icon": "play_arrow", "link": reverse_lazy("console:run")},
                {"title": "Live monitor", "icon": "grid_view", "link": reverse_lazy("console:monitor")},
            ]},
            {"title": "Results", "separator": True, "items": [
                {"title": "Session analytics", "icon": "bar_chart", "link": reverse_lazy("console:analytics")},
                {"title": "Compare sessions", "icon": "compare_arrows", "link": reverse_lazy("console:compare")},
                {"title": "Sessions", "icon": "history", "link": _ADMIN("scraper_session")},
                {"title": "Section results", "icon": "fact_check", "link": _ADMIN("scraper_sectionresult")},
            ]},
            {"title": "Centers", "separator": True, "items": [
                {"title": "Update from Kronos", "icon": "sync", "link": reverse_lazy("console:centers")},
                {"title": "Centers", "icon": "store", "link": _ADMIN("centers_center")},
                {"title": "Center groups", "icon": "workspaces", "link": _ADMIN("centers_centergroup")},
                {"title": "Center lists", "icon": "list_alt", "link": _ADMIN("centers_centerlist")},
                {"title": "Known empty", "icon": "block", "link": _ADMIN("centers_knownempty")},
                {"title": "Sync history", "icon": "update", "link": _ADMIN("centers_centersynclog")},
            ]},
            {"title": "Automation", "separator": True, "items": [
                {"title": "Automatic runs", "icon": "event_repeat", "link": _ADMIN("scraper_autorun")},
                {"title": "Schedules", "icon": "schedule", "link": _ADMIN("django_q_schedule")},
            ]},
            {"title": "System", "separator": True, "items": [
                {"title": "Jobs", "icon": "work", "link": _ADMIN("scraper_job")},
                {"title": "Queued tasks", "icon": "queue", "link": _ADMIN("django_q_ormq")},
                {"title": "Failed tasks", "icon": "error", "link": _ADMIN("django_q_failure")},
                {"title": "Settings", "icon": "settings", "link": reverse_lazy("admin:constance_config_changelist")},
            ]},
        ],
    },
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {"scraper": {"handlers": ["console"], "level": "INFO", "propagate": False}},
}
