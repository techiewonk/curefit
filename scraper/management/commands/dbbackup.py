"""
Make a safe copy of the database, even while the admin is running.

    python manage.py dbbackup                    # backups/db_YYYYMMDD.sqlite3
    python manage.py dbbackup --dir D:\\safe     # somewhere else
    python manage.py dbbackup --keep 10          # and keep only the 10 newest copies there

A plain file copy of db.sqlite3 can miss recent changes: SQLite keeps them in db.sqlite3-wal for a while.
This uses SQLite's own backup, which copies everything into one self-contained file.
To restore: stop the admin, replace db.sqlite3 with the copy, delete db.sqlite3-wal and db.sqlite3-shm.
"""
import os
import sqlite3
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone


def backup(target_dir, keep=None):
    """Copy the live database into target_dir/db_YYYYMMDD.sqlite3 (a second copy the same day gets the time too).
    Returns the path written."""
    if connection.vendor != "sqlite":
        raise CommandError("dbbackup only knows SQLite databases")
    source = str(connection.settings_dict["NAME"])
    if not os.path.exists(source):
        raise CommandError(f"No database at {source}")
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    now = timezone.localtime()
    target = target_dir / f"db_{now:%Y%m%d}.sqlite3"
    if target.exists():
        target = target_dir / f"db_{now:%Y%m%d-%H%M%S}.sqlite3"
    partial = target.with_suffix(".partial")
    partial.unlink(missing_ok=True)  # left by a copy that was cut off: start clean
    try:
        src = sqlite3.connect(source, timeout=60)
        try:
            dst = sqlite3.connect(partial)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        os.replace(partial, target)
    except (sqlite3.Error, OSError) as e:
        partial.unlink(missing_ok=True)  # never leave half a copy that looks like a backup
        raise CommandError(f"The backup failed ({e.__class__.__name__}: {e}); nothing was written. "
                           "Check the folder and the free disk space, then try again.") from e
    if keep:
        copies = sorted(target_dir.glob("db_*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in copies[keep:]:
            old.unlink()
    return target


class Command(BaseCommand):
    help = ("Safe copy of the database (includes changes still in the -wal file) into backups/db_YYYYMMDD.sqlite3. "
            "Works while the admin runs.")

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=str(settings.BASE_DIR / "backups"), help="folder for the copy (default backups/)")
        parser.add_argument("--keep", type=int, default=None, help="keep only this many newest copies in the folder")

    def handle(self, *args, **opts):
        if opts["keep"] is not None and opts["keep"] < 1:
            raise CommandError("--keep must be 1 or more")
        path = backup(opts["dir"], opts["keep"])
        self.stdout.write(f"Database copied to {path} ({path.stat().st_size // 1024} KB).")
