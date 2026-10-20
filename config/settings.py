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
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "import_export",
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
        "DIRS": [],
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
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
WHITENOISE_USE_FINDERS = True  # serve app static files directly: no collectstatic step locally
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
    "catch_up": False,
}

UNFOLD = {
    "SITE_TITLE": "Curefit Admin",
    "SITE_HEADER": "Curefit Admin",
    "SITE_SYMBOL": "monitoring",
    "SHOW_HISTORY": False,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
}
