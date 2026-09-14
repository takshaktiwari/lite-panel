"""Site backup service.

Creates a single timestamped archive per backup that contains:
  - files.tar.gz  — the site's home directory (root_dir)
  - {db_name}.sql.gz — mysqldump of every linked database (if any)

Archives are stored at:
  /var/backups/lite-panel/{site_name}/{site_name}_YYYY-MM-DD_HHMM.tar.gz
"""

from __future__ import annotations

import logging
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

BACKUP_ROOT = Path("/var/backups/lite-panel")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def backup_dir(site_name: str) -> Path:
    d = BACKUP_ROOT / site_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def backup_filename(site_name: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    return f"{site_name}_{ts}.tar.gz"


def list_backups(site_name: Optional[str] = None) -> List[dict]:
    """Return backup metadata dicts, newest first.

    If *site_name* is given, only that site's backups are returned.
    Each dict has: name, site, path, size_mb, created_at (datetime).
    """
    results = []

    search_root = BACKUP_ROOT / site_name if site_name else BACKUP_ROOT
    if not search_root.exists():
        return []

    pattern = "*.tar.gz"
    for f in sorted(search_root.rglob(pattern), key=lambda p: p.stat().st_mtime, reverse=True):
        # parent dir name == site_name
        site = f.parent.name
        stat = f.stat()
        results.append({
            "name": f.name,
            "site": site,
            "path": str(f),
            "size_mb": round(stat.st_size / 1024 / 1024, 2),
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        })

    return results


def delete_backup(site_name: str, filename: str) -> None:
    """Delete a backup file. Raises FileNotFoundError if not found."""
    # Sanitise: filename must not contain path separators
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError("Invalid backup filename")
    path = BACKUP_ROOT / site_name / filename
    if not path.exists():
        raise FileNotFoundError(f"Backup {filename} not found")
    path.unlink()
    logger.info("deleted backup %s", path)


def backup_path(site_name: str, filename: str) -> Path:
    """Resolve and validate a backup path for download."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError("Invalid backup filename")
    path = BACKUP_ROOT / site_name / filename
    if not path.exists():
        raise FileNotFoundError(f"Backup {filename} not found")
    return path


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

def create_backup(
    site_name: str,
    root_dir: str,
    db_names: List[str],
    ctx=None,
) -> Path:
    """Create a full site backup archive and return its path.

    *ctx* is a :class:`~app.jobs.JobContext` when running as a background job.

    Steps:
    1. Dump each linked database to a temp .sql.gz file.
    2. Create a tar.gz archive containing:
       - files/  →  the site's root_dir tree
       - db/{name}.sql.gz  →  one dump per database
    3. Move the archive to the backup directory.
    """
    def log(msg: str) -> None:
        if ctx:
            ctx.log(msg)
        else:
            logger.info(msg)

    dest_dir = backup_dir(site_name)
    archive_name = backup_filename(site_name)
    archive_path = dest_dir / archive_name

    log(f"Starting backup for site '{site_name}'")
    log(f"Output: {archive_path}")

    with tempfile.TemporaryDirectory(prefix="lp-backup-") as tmpdir:
        tmp = Path(tmpdir)
        db_dumps: List[Path] = []

        # --- Database dumps ------------------------------------------------
        if db_names:
            from app.services import databases as db_service
            dump_dir = tmp / "db"
            dump_dir.mkdir()
            for db_name in db_names:
                dump_file = dump_dir / f"{db_name}.sql.gz"
                log(f"Dumping database '{db_name}' → {dump_file.name}")
                try:
                    db_service.export_database(db_name, dump_file, gzip=True)
                    db_dumps.append(dump_file)
                    log(f"Database dump complete ({round(dump_file.stat().st_size/1024/1024,2)} MB)")
                except Exception as exc:
                    log(f"WARNING: could not dump '{db_name}': {exc}")

        # --- Files tar + combine -------------------------------------------
        log(f"Archiving site files from {root_dir} …")
        root_path = Path(root_dir)

        with tarfile.open(archive_path, "w:gz") as tar:
            # Site files
            if root_path.exists():
                tar.add(root_path, arcname="files")
                log("Site files added to archive")
            else:
                log(f"WARNING: root_dir {root_dir} does not exist, skipping files")

            # DB dumps
            for dump in db_dumps:
                tar.add(dump, arcname=f"db/{dump.name}")

        size_mb = round(archive_path.stat().st_size / 1024 / 1024, 2)
        log(f"Backup complete: {archive_name} ({size_mb} MB)")

    return archive_path
