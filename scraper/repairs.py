"""One-time repairs of stored data after a change in how it is computed.

Customers, the all-runs tables and the monthly table are built when a run finishes, so rows built by older code
keep the old results until they are recomputed. `run_once()` does that recomputation once per VERSION (a
RepairLog row remembers it) and is safe to call at every start: `serve` runs it in the background.

Order matters: everything is rebuilt from the per-run rows first, and only then are old per-run rows pruned
(Settings > Keep run rows), so a repair never loses data it still needs.
"""
import logging

from constance import config
from django.db import IntegrityError
from django.utils import timezone

from . import customers, masters, perfwide
from .models import GymPack, RepairLog, SalesPack, Session

log = logging.getLogger(__name__)

VERSION = "2026-10-data-fixes"  # change it when a new repair is needed: it then runs once more


def _drop_collapsed_no_id_rows():
    """All-runs rows without a user id were keyed only by center, pack and date, so different people were merged
    into one. Drop those whose session still has its rows: merging again rebuilds them one per person."""
    replayable = Session.objects.filter(rows_pruned=False)
    dropped = 0
    for model in (GymPack, SalesPack):
        dropped += model.objects.filter(user_id="", last_session__in=replayable).exclude(key__contains="~").delete()[0]
    return dropped


def run_once(force=False):
    """Run the repair unless this VERSION already ran. Returns the RepairLog row, or None when nothing was to do."""
    if not force and RepairLog.objects.filter(version=VERSION, error="").exists():
        return None
    counts, error = {}, ""
    try:
        counts["no_id_rows_dropped"] = _drop_collapsed_no_id_rows()
        counts["customers"] = customers.rebuild_all()
        counts["sessions_merged"] = len(masters.merge_all())
        counts["months"] = perfwide.rebuild_months()
        counts["rows_pruned"] = masters.prune_run_rows(config.KEEP_RUN_ROWS)
    except Exception as e:  # noqa: BLE001 - logged and retried at the next start, never breaks the start-up
        log.exception("data repair %s failed", VERSION)
        error = f"{type(e).__name__}: {e}"[:2000]
    try:
        entry, _ = RepairLog.objects.update_or_create(
            version=VERSION, defaults={"counts": counts, "error": error, "ran_at": timezone.now()})
    except IntegrityError:  # two starts at once: the other one logged it
        entry = RepairLog.objects.filter(version=VERSION).first()
    return entry
