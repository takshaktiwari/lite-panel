"""RAM-aware tuning.

The formulas come from the original setup scripts, where they were applied
once with ``sed`` and then frozen.  Here they are *computed* instead: the panel
recalculates them from the machine's actual RAM, shows the result, lets the
operator override any single value, and re-applies everything on demand -- so
resizing the server is a re-render rather than a re-install.

Every function returns plain values, which makes them testable on a
workstation and keeps the arithmetic out of the templates.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from app.services import system

logger = logging.getLogger(__name__)


def _overrides() -> Dict[str, str]:
    """Operator overrides from the database, if it is reachable."""
    try:
        from sqlalchemy import select

        from app.database import session_scope
        from app.models import TuningOverride

        with session_scope() as db:
            return {row.key: row.value for row in db.scalars(select(TuningOverride)).all()}
    except Exception as exc:  # noqa: BLE001 - tuning must work before the DB exists
        logger.debug("no tuning overrides available: %s", exc)
        return {}


def _apply(values: Dict, prefix: str) -> Dict:
    """Overlay any stored overrides onto computed values."""
    stored = _overrides()
    for key in list(values):
        override = stored.get(f"{prefix}.{key}")
        if override is not None:
            values[key] = _coerce(override, values[key])
    return values


def _coerce(raw: str, like):
    if isinstance(like, int):
        try:
            return int(raw)
        except ValueError:
            return like
    return raw


# --------------------------------------------------------------------------
# PHP
# --------------------------------------------------------------------------


def php_settings(total_ram_mb: Optional[int] = None) -> Dict:
    """php.ini values for the panel's drop-in.

    Tiers follow the original main.sh: small boxes get a memory limit that
    leaves room for MariaDB and the web server beside PHP.
    """
    ram = total_ram_mb or system.total_ram_mb()

    if ram <= 1024:
        memory_limit, opcache_mb = "128M", 64
    elif ram <= 2048:
        memory_limit, opcache_mb = "256M", 128
    else:
        memory_limit, opcache_mb = "512M", 256

    return _apply(
        {
            "memory_limit": memory_limit,
            "opcache_memory_consumption": opcache_mb,
            "opcache_max_accelerated_files": 20000,
            "opcache_validate_timestamps": 1,
            "post_max_size": "128M",
            "upload_max_filesize": "128M",
            "max_execution_time": 300,
            "max_input_time": 300,
        },
        "php",
    )


def fpm_pool_settings(total_ram_mb: Optional[int] = None) -> Dict:
    """PHP-FPM process manager settings.

    ``ondemand`` on smaller boxes because idle workers on a 1GB instance cost
    memory that MariaDB needs more; ``dynamic`` once there is room to keep
    workers warm.
    """
    ram = total_ram_mb or system.total_ram_mb()

    if ram <= 1024:
        mode, max_children = "ondemand", 6
    elif ram <= 2048:
        mode, max_children = "ondemand", 12
    elif ram <= 4096:
        mode, max_children = "ondemand", 24
    else:
        mode, max_children = "dynamic", 40

    return _apply(
        {
            "pm": mode,
            "max_children": max_children,
            "process_idle_timeout": "10s",
            "max_requests": 500,
            "start_servers": 8,
            "min_spare_servers": 4,
            "max_spare_servers": 16,
        },
        "fpm",
    )


# --------------------------------------------------------------------------
# MariaDB
# --------------------------------------------------------------------------


def mariadb_settings(total_ram_mb: Optional[int] = None) -> Dict:
    """InnoDB sizing.

    MariaDB is given at most half the machine's RAM, and the buffer pool 60%
    of that -- the rest of the box still has to run PHP workers and nginx.
    The floors matter more than the percentages on a 1GB instance, where a
    naive percentage would produce a buffer pool too small to start.
    """
    ram = total_ram_mb or system.total_ram_mb()

    db_ram = max(ram // 2, 128)
    buffer_pool = max(db_ram * 60 // 100, 64)
    tmp_table = max(db_ram * 5 // 100, 8)
    log_file = max(db_ram * 15 // 100, 32)
    max_connections = 50 if ram >= 1024 else 20

    return _apply(
        {
            "innodb_buffer_pool_size_mb": buffer_pool,
            "innodb_log_file_size_mb": log_file,
            "innodb_log_buffer_size_mb": 8,
            "tmp_table_size_mb": tmp_table,
            "max_connections": max_connections,
            "thread_cache_size": 16,
            "table_open_cache": 400,
            "open_files_limit": 1024,
            "wait_timeout": 300,
            "interactive_timeout": 300,
            "long_query_time": 2,
        },
        "mariadb",
    )


# --------------------------------------------------------------------------
# Swap
# --------------------------------------------------------------------------


def swap_recommendation_mb(total_ram_mb: Optional[int] = None) -> int:
    """How much swap this machine should have.

    Capped at 2GB: swap here is insurance against an OOM kill during a package
    install or a MariaDB start, not a substitute for RAM.
    """
    ram = total_ram_mb or system.total_ram_mb()
    if ram <= 512:
        return 1024
    return 2048


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def summary() -> Dict:
    """Everything the tuning page shows, with the RAM it was derived from."""
    ram = system.total_ram_mb()
    return {
        "total_ram_mb": ram,
        "php": php_settings(ram),
        "fpm": fpm_pool_settings(ram),
        "mariadb": mariadb_settings(ram),
        "swap_recommendation_mb": swap_recommendation_mb(ram),
        "overrides": _overrides(),
    }
