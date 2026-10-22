"""Where a gym pack stands today: starts later, active, ending soon, paused, ended (or no dates).

One order decides when two apply: an ended pack is "ended" even if it was paused, and a pack that has not
started yet is "starts later" (a future member) whatever its status says. "Ending soon" uses the same number of
days as the leads (Settings → Leads → LEAD_EXPIRING_DAYS).
"""
import datetime as dt

from constance import config as settings
from django.db.models import Q
from django.utils import timezone

ENDED, UPCOMING, PAUSED, ENDING, ACTIVE, UNKNOWN = "ended", "upcoming", "paused", "ending", "active", "unknown"
CHOICES = [(UPCOMING, "Starts later"), (ACTIVE, "Active"), (ENDING, "Ending soon"), (PAUSED, "Paused"),
           (ENDED, "Ended"), (UNKNOWN, "No dates")]
LABELS = dict(CHOICES)
COLOURS = {UPCOMING: "primary", ACTIVE: "success", ENDING: "warning", PAUSED: "info", ENDED: "danger"}


def _soon(today):
    return today + dt.timedelta(days=settings.LEAD_EXPIRING_DAYS)


def state(start, end, status, today=None):
    today = today or timezone.localdate()
    if end and end < today:
        return ENDED
    if start and start > today:
        return UPCOMING
    if (status or "").strip().lower() == "paused":
        return PAUSED
    if not end:
        return UNKNOWN
    return ENDING if end <= _soon(today) else ACTIVE


def q(name, prefix="", today=None):
    """The same rule as state(), as a filter. prefix: e.g. "packs__" to filter another model through a relation."""
    today = today or timezone.localdate()
    f = lambda **kw: Q(**{prefix + k: v for k, v in kw.items()})  # noqa: E731
    ended = f(pack_end_date__lt=today)
    upcoming = f(pack_start_date__gt=today) & ~ended
    paused = f(status__iexact="paused") & ~ended & ~upcoming
    rest = ~ended & ~upcoming & ~paused
    return {ENDED: ended, UPCOMING: upcoming, PAUSED: paused,
            UNKNOWN: rest & f(pack_end_date__isnull=True),
            ENDING: rest & f(pack_end_date__gte=today, pack_end_date__lte=_soon(today)),
            ACTIVE: rest & f(pack_end_date__gt=_soon(today))}[name]


def date_class(kind, day, today=None):
    """Text colour for a start or end date: a start still to come, an end already past or coming soon."""
    if not day:
        return ""
    today = today or timezone.localdate()
    if kind == "start" and day > today:
        return "font-semibold text-primary-700 dark:text-primary-400"
    if kind == "end" and day < today:
        return "font-semibold text-red-700 dark:text-red-400"
    if kind == "end" and day <= _soon(today):
        return "font-semibold text-orange-700 dark:text-orange-400"
    return ""
