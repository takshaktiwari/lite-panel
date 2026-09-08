"""Config editor service.

Provides safe read/write for system config files the panel exposes for
editing.  Every save is backup-first; validation runs before the new file
is committed; the service is reloaded; and if anything fails the backup
is restored automatically.

Scope (v1):
  - /etc/php/<version>/fpm/php.ini     (per installed PHP version)
  - /etc/nginx/nginx.conf               (nginx global)
  - /etc/nginx/custom/lite-panel-<n>-custom.conf  (per-site override)
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_BACKUPS = 5
NGINX_CUSTOM_DIR = Path("/etc/nginx/custom")
NGINX_CONF = Path("/etc/nginx/nginx.conf")


def php_ini_path(version: str) -> Path:
    return Path(f"/etc/php/{version}/fpm/php.ini")


def site_override_path(name: str) -> Path:
    NGINX_CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    return NGINX_CUSTOM_DIR / f"lite-panel-{name}-custom.conf"


def read_config(path: Path) -> str:
    path = Path(path)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def backup_config(path: Path) -> Optional[Path]:
    path = Path(path)
    if not path.exists():
        return None
    existing = sorted(
        path.parent.glob(f"{path.name}.backup.*"),
        key=lambda p: int(p.suffix.lstrip(".")) if p.suffix.lstrip(".").isdigit() else 0,
    )
    if existing:
        last = int(existing[-1].suffix.lstrip(".")) if existing[-1].suffix.lstrip(".").isdigit() else 0
        next_idx = (last + 1) % MAX_BACKUPS
    else:
        next_idx = 0
    backup = path.parent / f"{path.name}.backup.{next_idx}"
    shutil.copy2(str(path), str(backup))
    logger.info("backed up %s -> %s", path, backup)
    return backup


def list_backups(path: Path) -> List[Path]:
    path = Path(path)
    candidates = sorted(
        path.parent.glob(f"{path.name}.backup.*"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    return candidates


def restore_latest_backup(path: Path) -> bool:
    backups = list_backups(path)
    if not backups:
        logger.warning("no backup found for %s; cannot restore", path)
        return False
    shutil.copy2(str(backups[0]), str(path))
    logger.info("restored %s from %s", path, backups[0])
    return True


def validate_nginx() -> Tuple[bool, str]:
    try:
        result = subprocess.run(
            ["nginx", "-t"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (result.stderr or result.stdout).strip()
        return result.returncode == 0, output
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def validate_php_ini(version: str, path: Path) -> Tuple[bool, str]:
    php_bin = f"php{version}"
    try:
        result = subprocess.run(
            [php_bin, "-r", f"if (!parse_ini_file('{path}')) {{ die('INVALID'); }}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "php.ini parse failed").strip()
        return True, "php.ini parsed OK"
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def reload_nginx() -> Tuple[bool, str]:
    ok, msg = validate_nginx()
    if not ok:
        return False, f"nginx -t failed: {msg}"
    try:
        result = subprocess.run(
            ["systemctl", "reload", "nginx"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "reload failed").strip()
        return True, "nginx reloaded"
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def reload_php_fpm(version: str) -> Tuple[bool, str]:
    service = f"php{version}-fpm"
    try:
        result = subprocess.run(
            ["systemctl", "reload", service],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or f"{service} reload failed").strip()
        return True, f"{service} reloaded"
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


class SaveError(Exception):
    """Raised when save fails; message is human-readable."""


def save_nginx_conf(content: str) -> str:
    return _save_with_rollback(
        path=NGINX_CONF,
        content=content,
        validator=validate_nginx,
        reloader=reload_nginx,
        label="nginx.conf",
    )


def save_site_override(name: str, content: str) -> str:
    path = site_override_path(name)
    return _save_with_rollback(
        path=path,
        content=content,
        validator=validate_nginx,
        reloader=reload_nginx,
        label=f"nginx override for {name}",
    )


def save_php_ini(version: str, content: str) -> str:
    path = php_ini_path(version)

    def _validate() -> Tuple[bool, str]:
        return validate_php_ini(version, path)

    def _reload() -> Tuple[bool, str]:
        return reload_php_fpm(version)

    return _save_with_rollback(
        path=path,
        content=content,
        validator=_validate,
        reloader=_reload,
        label=f"php{version} php.ini",
    )


def _save_with_rollback(*, path: Path, content: str, validator, reloader, label: str) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    backup = backup_config(path)

    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        if backup:
            restore_latest_backup(path)
        raise SaveError(f"Could not write {label}: {exc}") from exc

    ok, msg = validator()
    if not ok:
        if backup:
            restore_latest_backup(path)
        raise SaveError(f"Validation failed — changes reverted.\n\n{msg}")

    ok, msg = reloader()
    if not ok:
        if backup:
            restore_latest_backup(path)
        raise SaveError(f"Service reload failed — changes reverted.\n\n{msg}")

    logger.info("saved and reloaded %s", label)
    return msg
