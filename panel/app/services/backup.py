"""Site backup service.

Creates high-compression (gzip level 9) timestamped archives containing:
  - manifest.json      — metadata about the backup (timestamp, site, scope)
  - files/             — site root directory (if include_files is True)
  - db/{db_name}.sql   — raw text SQL dump of each linked database (if include_db is True)

Archives are stored at:
  /var/backups/lite-panel/{site_name}/{filename}.tar.gz
"""

from __future__ import annotations

import json
import logging
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession, selectinload

from app.config import get_settings
from app.models import BackupSchedule, Site, SiteDatabase, utcnow

logger = logging.getLogger(__name__)

BACKUP_ROOT = Path("/var/backups/lite-panel")


def get_backup_root() -> Path:
    # Allows monkeypatching BACKUP_ROOT in tests, while defaulting to settings
    if BACKUP_ROOT != Path("/var/backups/lite-panel"):
        return BACKUP_ROOT
    try:
        return get_settings().backup_dir
    except Exception:
        return BACKUP_ROOT


WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


# ---------------------------------------------------------------------------
# Helpers & File Management
# ---------------------------------------------------------------------------

def backup_dir(site_name: str) -> Path:
    d = get_backup_root() / site_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def backup_filename(site_name: str, include_files: bool = True, include_db: bool = True) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    if include_files and include_db:
        scope = "full"
    elif include_files:
        scope = "files"
    elif include_db:
        scope = "db"
    else:
        scope = "empty"
    return f"{site_name}_{scope}_{ts}.tar.gz"


def parse_backup_scope(filename: str) -> str:
    """Infer the backup scope (Full, Files, Database) from the archive name.

    Archive format: {site_name}_{scope}_{YYYY-MM-DD}_{HHMM}.tar.gz
    Since site_name may contain underscores (e.g. asiatrade_bee1_online),
    the scope tag is reliably positioned at parts[-3].
    """
    base = filename.replace(".tar.gz", "")
    parts = base.split("_")
    if len(parts) >= 3:
        tag = parts[-3].lower()
        if tag == "full":
            return "Full (Files + DB)"
        if tag == "files":
            return "Files only"
        if tag == "db":
            return "Database only"

    if "_db_" in filename:
        return "Database only"
    if "_files_" in filename:
        return "Files only"
    if "_full_" in filename:
        return "Full (Files + DB)"

    return "Full (Files + DB)"


def list_backups(site_name: Optional[str] = None) -> List[dict]:
    """Return backup metadata dicts, newest first.

    If *site_name* is given, only that site's backups are returned.
    Each dict has: name, site, path, size_mb, scope, created_at (datetime).
    """
    results = []

    root = get_backup_root()
    search_root = root / site_name if site_name else root
    try:
        if not search_root.exists():
            return []
    except (OSError, PermissionError):
        return []

    pattern = "*.tar.gz"
    try:
        files = sorted(search_root.rglob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    except (OSError, PermissionError):
        return []

    for f in files:
        site = f.parent.name
        try:
            stat = f.stat()
        except OSError:
            continue
        results.append({
            "name": f.name,
            "site": site,
            "path": str(f),
            "scope": parse_backup_scope(f.name),
            "size_mb": round(stat.st_size / 1024 / 1024, 2),
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        })

    return results


def delete_backup(site_name: str, filename: str) -> None:
    """Delete a backup file. Raises FileNotFoundError if not found."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError("Invalid backup filename")
    path = get_backup_root() / site_name / filename
    if not path.exists():
        raise FileNotFoundError(f"Backup {filename} not found")
    path.unlink()
    logger.info("Deleted backup %s", path)


def prune_backups(site_name: str, keep_count: int, scope: Optional[str] = None, ctx=None) -> int:
    """Retain only the newest *keep_count* backup archives for a site.

    If *scope* is given (e.g. "db", "files", "full"), only backups of that scope
    are counted towards the retention limit.
    Deletes the oldest excess archives and returns the count of deleted archives.
    """
    def log(msg: str) -> None:
        if ctx:
            ctx.log(msg)
        else:
            logger.info(msg)

    if keep_count <= 0:
        return 0

    all_backups = list_backups(site_name)
    if scope:
        scope_key = scope.lower()
        if scope_key == "db":
            target_scope = "Database only"
        elif scope_key == "files":
            target_scope = "Files only"
        elif scope_key == "full":
            target_scope = "Full (Files + DB)"
        else:
            target_scope = scope
        matching = [b for b in all_backups if b["scope"] == target_scope]
    else:
        matching = all_backups

    if len(matching) <= keep_count:
        return 0

    to_prune = matching[keep_count:]
    deleted = 0
    for b in to_prune:
        try:
            delete_backup(site_name, b["name"])
            deleted += 1
            log(f"Retention policy: deleted older backup {b['name']} (max allowed: {keep_count})")
        except Exception as exc:
            logger.warning("Failed to delete old backup %s during rotation: %s", b["name"], exc)

    if deleted:
        log(f"Retention cleanup complete: pruned {deleted} older backup(s) to match limit of {keep_count}.")
    return deleted


def backup_path(site_name: str, filename: str) -> Path:
    """Resolve and validate a backup path for download."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError("Invalid backup filename")
    path = get_backup_root() / site_name / filename
    if not path.exists():
        raise FileNotFoundError(f"Backup {filename} not found")
    return path


# ---------------------------------------------------------------------------
# Creation (High-compression Single Archive)
# ---------------------------------------------------------------------------

def create_backup(
    site_name: str,
    root_dir: str,
    db_names: List[str],
    include_files: bool = True,
    include_db: bool = True,
    ctx=None,
) -> Path:
    """Create a high-compression site backup archive and return its path.

    *ctx* is a :class:`~app.jobs.JobContext` when running as a background job.
    Uses gzip compresslevel=9 for maximum compression. Raw text SQL is bundled
    directly into the tar stream so the level-9 gzip compresses both files and
    database dumps together efficiently.
    """
    def log(msg: str) -> None:
        if ctx:
            ctx.log(msg)
        else:
            logger.info(msg)

    if not include_files and not include_db:
        raise ValueError("Cannot create backup: neither files nor database selected.")

    dest_dir = backup_dir(site_name)
    archive_name = backup_filename(site_name, include_files=include_files, include_db=include_db)
    archive_path = dest_dir / archive_name

    scope_str = "Files + Database" if include_files and include_db else ("Files only" if include_files else "Database only")
    log(f"Starting backup for site '{site_name}' ({scope_str})")
    log(f"Archive output: {archive_path}")

    with tempfile.TemporaryDirectory(prefix="lp-backup-") as tmpdir:
        tmp = Path(tmpdir)
        db_dumps: List[Path] = []

        # --- 1. Database dumps (uncompressed text for outer gzip level 9) ---
        if include_db and db_names:
            from app.services import databases as db_service
            dump_dir = tmp / "db"
            dump_dir.mkdir(parents=True, exist_ok=True)
            for db_name in db_names:
                dump_file = dump_dir / f"{db_name}.sql"
                log(f"Dumping database '{db_name}' → {dump_file.name}")
                try:
                    db_service.export_database(db_name, dump_file, gzip=False)
                    db_dumps.append(dump_file)
                    size_mb = round(dump_file.stat().st_size / 1024 / 1024, 2)
                    log(f"Database dump '{db_name}' complete ({size_mb} MB uncompressed)")
                except Exception as exc:
                    log(f"WARNING: could not dump '{db_name}': {exc}")
        elif include_db and not db_names:
            log("No databases linked to this site; skipping database export")

        # --- 2. Manifest file ---
        manifest_file = tmp / "manifest.json"
        manifest_data = {
            "site_name": site_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scope": "full" if include_files and include_db else ("files" if include_files else "db"),
            "include_files": include_files,
            "include_db": include_db,
            "databases": db_names if include_db else [],
            "compression": "gzip-9",
        }
        manifest_file.write_text(json.dumps(manifest_data, indent=2))

        # --- 3. Build single high-compression archive ---
        log("Packing and compressing archive with gzip level 9 (maximum compression)…")
        root_path = Path(root_dir)

        # Use temporary file first to prevent incomplete archives if interrupted
        tmp_archive = tmp / archive_name
        with tarfile.open(tmp_archive, mode="w:gz", compresslevel=9) as tar:
            # Manifest
            tar.add(manifest_file, arcname="manifest.json")

            # Site files
            if include_files:
                if root_path.exists():
                    log(f"Archiving files from {root_dir}…")
                    tar.add(root_path, arcname="files")
                    log("Site files added")
                else:
                    log(f"WARNING: root_dir '{root_dir}' does not exist, skipping files")

            # DB dumps
            if include_db:
                for dump in db_dumps:
                    tar.add(dump, arcname=f"db/{dump.name}")
                if db_dumps:
                    log(f"Added {len(db_dumps)} database dump(s) to archive")

        # Move to destination
        tmp_archive.replace(archive_path)
        size_mb = round(archive_path.stat().st_size / 1024 / 1024, 2)
        log(f"✓ Backup complete: {archive_name} ({size_mb} MB)")

    return archive_path


# ---------------------------------------------------------------------------
# Schedule Helpers & Calculations
# ---------------------------------------------------------------------------

def describe_schedule(schedule: BackupSchedule) -> str:
    """Format human-readable schedule description."""
    freq = schedule.frequency
    h = f"{schedule.hour:02d}:{schedule.minute:02d}"

    if freq == "twice_daily":
        h2 = f"{(schedule.hour + 12) % 24:02d}:{schedule.minute:02d}"
        return f"Twice daily at {h} & {h2} UTC"
    if freq == "weekly":
        day_name = WEEKDAY_NAMES[schedule.day_of_week % 7]
        return f"Weekly on {day_name} at {h} UTC"
    if freq == "monthly":
        suffix = "th"
        d = schedule.day_of_month
        if d in [1, 21, 31]:
            suffix = "st"
        elif d in [2, 22]:
            suffix = "nd"
        elif d in [3, 23]:
            suffix = "rd"
        return f"Monthly on day {d}{suffix} at {h} UTC"
    return f"Daily at {h} UTC"


def get_next_run(schedule: BackupSchedule, from_time: Optional[datetime] = None) -> datetime:
    """Calculate the next datetime (UTC) the schedule is scheduled to run."""
    now = from_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    freq = schedule.frequency
    h = schedule.hour
    m = schedule.minute

    if freq == "daily":
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if freq == "twice_daily":
        target_hours = sorted([h, (h + 12) % 24])
        for target_h in target_hours:
            candidate = now.replace(hour=target_h, minute=m, second=0, microsecond=0)
            if candidate > now:
                return candidate
        # None remaining today, return first target tomorrow
        return now.replace(hour=target_hours[0], minute=m, second=0, microsecond=0) + timedelta(days=1)

    if freq == "weekly":
        days_ahead = (schedule.day_of_week - now.weekday()) % 7
        candidate = (now + timedelta(days=days_ahead)).replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    if freq == "monthly":
        target_day = min(schedule.day_of_month, 28)
        try:
            candidate = now.replace(day=target_day, hour=h, minute=m, second=0, microsecond=0)
        except ValueError:
            candidate = now.replace(day=28, hour=h, minute=m, second=0, microsecond=0)

        if candidate <= now:
            month = now.month + 1
            year = now.year
            if month > 12:
                month = 1
                year += 1
            candidate = candidate.replace(year=year, month=month)
        return candidate

    return now + timedelta(days=1)


def check_schedule_due(schedule: BackupSchedule, now: Optional[datetime] = None) -> bool:
    """Return True if the schedule is currently due to run."""
    if not schedule.is_enabled:
        return False

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    last_run = schedule.last_run_at
    if last_run and last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)

    freq = schedule.frequency
    h = schedule.hour
    m = schedule.minute

    if freq == "daily":
        target = current.replace(hour=h, minute=m, second=0, microsecond=0)
        if current >= target:
            return last_run is None or last_run < target
        return False

    if freq == "twice_daily":
        target_hours = sorted([h, (h + 12) % 24])
        past_targets = [
            current.replace(hour=th, minute=m, second=0, microsecond=0)
            for th in target_hours
            if current >= current.replace(hour=th, minute=m, second=0, microsecond=0)
        ]
        if past_targets:
            latest_target = max(past_targets)
            return last_run is None or last_run < latest_target
        return False

    if freq == "weekly":
        if current.weekday() == (schedule.day_of_week % 7):
            target = current.replace(hour=h, minute=m, second=0, microsecond=0)
            if current >= target:
                return last_run is None or last_run < target
        return False

    if freq == "monthly":
        target_day = min(schedule.day_of_month, 28)
        if current.day == target_day:
            target = current.replace(hour=h, minute=m, second=0, microsecond=0)
            if current >= target:
                return last_run is None or last_run < target
        return False

    return False


def poll_and_run_schedules(db: OrmSession) -> int:
    """Find due backup schedules and enqueue them as background jobs.

    Returns the count of enqueued jobs.
    """
    from app.jobs import enqueue

    schedules = db.scalars(
        select(BackupSchedule)
        .where(BackupSchedule.is_enabled == True)
        .options(selectinload(BackupSchedule.site))
    ).all()

    now = datetime.now(timezone.utc)
    enqueued_count = 0

    for s in schedules:
        if not s.site or not s.site.is_active:
            continue

        if check_schedule_due(s, now):
            scope_desc = "Files + DB" if s.include_files and s.include_db else ("Files" if s.include_files else "DB")
            logger.info("Triggering scheduled backup for %s (%s, schedule #%d)", s.site.domain, scope_desc, s.id)
            try:
                enqueue(
                    db,
                    "backup.create",
                    f"Scheduled backup ({scope_desc}) for {s.site.domain}",
                    payload={
                        "site_id": s.site_id,
                        "include_files": s.include_files,
                        "include_db": s.include_db,
                        "schedule_id": s.id,
                        "keep_count": s.keep_count,
                    },
                )
                s.last_run_at = now
                db.commit()
                enqueued_count += 1
            except Exception as exc:
                logger.error("Failed to enqueue scheduled backup for schedule %d: %s", s.id, exc)

    return enqueued_count


# ---------------------------------------------------------------------------
# Background Scheduler Worker
# ---------------------------------------------------------------------------

_scheduler_stop = threading.Event()
_scheduler_thread: Optional[threading.Thread] = None


def _scheduler_worker():
    """Background thread polling every 30 seconds for due backup schedules."""
    from app.database import session_scope

    logger.info("Backup scheduler started")
    while not _scheduler_stop.is_set():
        try:
            with session_scope() as db:
                poll_and_run_schedules(db)
        except Exception as exc:
            logger.error("Error in backup scheduler loop: %s", exc)

        for _ in range(30):
            if _scheduler_stop.is_set():
                break
            time.sleep(1)

    logger.info("Backup scheduler stopped")


def start_backup_scheduler():
    global _scheduler_thread, _scheduler_stop
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_worker, daemon=True, name="backup-scheduler")
    _scheduler_thread.start()


def stop_backup_scheduler():
    global _scheduler_stop, _scheduler_thread
    _scheduler_stop.set()
    if _scheduler_thread and _scheduler_thread.is_alive():
        _scheduler_thread.join(timeout=3)
