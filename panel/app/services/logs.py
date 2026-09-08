"""Logs viewing service: safe tailing of server and site logs.

Restricts file reading strictly to an allowlist of system and site log files,
preventing arbitrary file exposure while providing instant troubleshooting.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from app.shell import run
from app.validators import ValidationError, validate_site_name

logger = logging.getLogger(__name__)

ALLOWED_LOG_DIRS = (
    Path("/var/log/nginx"),
    Path("/var/log/php"),
    Path("/var/log/mysql"),
    Path("/var/log/lite-panel"),
    Path("/var/log"),
)

ALLOWED_SERVICES = frozenset({"nginx", "mariadb", "vsftpd", "lite-panel"})


@dataclass
class LogTarget:
    id: str
    label: str
    category: str  # "site" or "service"
    log_type: str  # "error", "access", "journal", "slow", etc.
    file_path: Optional[Path] = None
    service_name: Optional[str] = None


def get_service_log_targets() -> List[LogTarget]:
    """Predefined service log targets."""
    return [
        LogTarget(
            id="nginx_error",
            label="Nginx Error Log",
            category="service",
            log_type="error",
            file_path=Path("/var/log/nginx/error.log"),
        ),
        LogTarget(
            id="nginx_access",
            label="Nginx Access Log",
            category="service",
            log_type="access",
            file_path=Path("/var/log/nginx/access.log"),
        ),
        LogTarget(
            id="nginx_journal",
            label="Nginx Service Journal",
            category="service",
            log_type="journal",
            service_name="nginx",
        ),
        LogTarget(
            id="mariadb_journal",
            label="MariaDB Service Journal",
            category="service",
            log_type="journal",
            service_name="mariadb",
        ),
        LogTarget(
            id="mariadb_slow",
            label="MariaDB Slow Query Log",
            category="service",
            log_type="slow",
            file_path=Path("/var/log/mysql/slow.log"),
        ),
        LogTarget(
            id="panel_log",
            label="Lite-Panel Application Log",
            category="service",
            log_type="panel",
            file_path=Path("/var/log/lite-panel/panel.log"),
        ),
        LogTarget(
            id="panel_nginx_error",
            label="Lite-Panel Nginx Error",
            category="service",
            log_type="error",
            file_path=Path("/var/log/lite-panel/nginx-error.log"),
        ),
        LogTarget(
            id="panel_journal",
            label="Lite-Panel Service Journal",
            category="service",
            log_type="journal",
            service_name="lite-panel",
        ),
        LogTarget(
            id="vsftpd_log",
            label="FTP Server Log",
            category="service",
            log_type="ftp",
            file_path=Path("/var/log/vsftpd.log"),
        ),
    ]


def get_site_log_targets(site_name: str) -> List[LogTarget]:
    """Log targets for a specific site."""
    clean_name = validate_site_name(site_name)
    return [
        LogTarget(
            id=f"site_{clean_name}_error",
            label="Nginx Error Log",
            category="site",
            log_type="error",
            file_path=Path(f"/var/log/nginx/{clean_name}.error.log"),
        ),
        LogTarget(
            id=f"site_{clean_name}_access",
            label="Nginx Access Log",
            category="site",
            log_type="access",
            file_path=Path(f"/var/log/nginx/{clean_name}.access.log"),
        ),
        LogTarget(
            id=f"site_{clean_name}_php_error",
            label="PHP Error Log",
            category="site",
            log_type="php_error",
            file_path=Path(f"/var/log/php/{clean_name}.error.log"),
        ),
    ]


def _is_path_allowed(path: Path) -> bool:
    try:
        resolved = Path(os.path.realpath(str(path)))
        for allowed_dir in ALLOWED_LOG_DIRS:
            allowed_real = Path(os.path.realpath(str(allowed_dir)))
            if resolved == allowed_real or allowed_real in resolved.parents:
                return True
    except Exception:
        return False
    return False


def tail_file(path: Path, lines: int = 100) -> str:
    """Read the last N lines of an allowed log file."""
    lines = max(1, min(lines, 1000))
    if not _is_path_allowed(path):
        raise ValidationError(f"Access to path {path} is not permitted.")

    if not path.exists():
        return f"[Log file '{path}' does not exist or has not been created yet.]"

    try:
        # Use tail command if available for speed and memory efficiency
        tail_cmd = shutil.which("tail")
        if tail_cmd:
            res = run([tail_cmd, "-n", str(lines), str(path)], check=False)
            if res.returncode == 0:
                output = res.stdout.strip()
                return output if output else "[Log file is empty.]"

        # Fallback in Python if tail is not available
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.readlines()
            tail_lines = content[-lines:]
            output = "".join(tail_lines).strip()
            return output if output else "[Log file is empty.]"
    except Exception as exc:
        logger.warning("Error reading log file %s: %s", path, exc)
        return f"[Error reading log file: {exc}]"


def tail_journal(service: str, lines: int = 100) -> str:
    """Read the last N lines of systemd journal for an allowed service."""
    lines = max(1, min(lines, 1000))
    # Allow matching php-fpm versions like php8.3-fpm
    if service not in ALLOWED_SERVICES and not (service.startswith("php") and service.endswith("-fpm")):
        raise ValidationError(f"Access to journal for service '{service}' is not permitted.")

    journalctl = shutil.which("journalctl")
    if not journalctl:
        return "[journalctl is not available on this system.]"

    try:
        res = run([journalctl, "-u", service, "-n", str(lines), "--no-pager"], check=False)
        if res.returncode == 0:
            output = res.stdout.strip()
            return output if output else f"[No journal entries found for {service}.]"
        return f"[journalctl exited with code {res.returncode}: {res.stderr}]"
    except Exception as exc:
        logger.warning("Error reading journal for %s: %s", service, exc)
        return f"[Error fetching journal: {exc}]"
