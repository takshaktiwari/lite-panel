"""Server monitoring and statistics service.

Provides:
1. Live top-like metrics: CPU, memory, swap, disk, network I/O, load, top processes.
2. Historical snapshots: background recording with automatic 7-day retention trimming.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import psutil
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as OrmSession

from app.models import ServerMetric, utcnow

logger = logging.getLogger(__name__)

# State for network rate calculation
_last_net_time: float = 0.0
_last_net_io = None


def get_live_metrics() -> Dict:
    """Collect instantaneous system metrics."""
    global _last_net_time, _last_net_io

    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")

    try:
        load = psutil.getloadavg()
    except (AttributeError, OSError):
        load = (0.0, 0.0, 0.0)

    # Network rate calculation
    now = time.time()
    curr_net = psutil.net_io_counters()
    rx_rate_kb = 0
    tx_rate_kb = 0

    if _last_net_io and _last_net_time and now > _last_net_time:
        dt = now - _last_net_time
        if dt > 0:
            rx_rate_kb = int((curr_net.bytes_recv - _last_net_io.bytes_recv) / dt / 1024)
            tx_rate_kb = int((curr_net.bytes_sent - _last_net_io.bytes_sent) / dt / 1024)

    _last_net_time = now
    _last_net_io = curr_net

    # CPU percent
    cpu_percent = psutil.cpu_percent(interval=0.0)
    per_cpu = psutil.cpu_percent(interval=0.0, percpu=True)

    try:
        boot_time = psutil.boot_time()
        uptime = int(now - boot_time)
    except Exception:
        uptime = 0

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": uptime,
        "cpu": {
            "percent": cpu_percent,
            "cores": psutil.cpu_count(logical=True) or 1,
            "per_core": per_cpu,
        },
        "memory": {
            "total_mb": int(mem.total / 1024 / 1024),
            "used_mb": int((mem.total - mem.available) / 1024 / 1024),
            "available_mb": int(mem.available / 1024 / 1024),
            "percent": round((mem.total - mem.available) / mem.total * 100, 1) if mem.total else 0.0,
        },
        "swap": {
            "total_mb": int(swap.total / 1024 / 1024),
            "used_mb": int(swap.used / 1024 / 1024),
            "percent": round(swap.used / swap.total * 100, 1) if swap.total else 0.0,
        },
        "disk": {
            "total_mb": int(disk.total / 1024 / 1024),
            "used_mb": int(disk.used / 1024 / 1024),
            "free_mb": int(disk.free / 1024 / 1024),
            "percent": disk.percent,
        },
        "load": [round(v, 2) for v in load],
        "network": {
            "rx_kb_s": max(0, rx_rate_kb),
            "tx_kb_s": max(0, tx_rate_kb),
        },
    }


def get_top_processes(limit: int = 15, sort_by: str = "cpu") -> List[Dict]:
    """Return top running processes sorted by cpu or memory."""
    procs = []
    attrs = ["pid", "name", "username", "cpu_percent", "memory_percent", "memory_info", "status"]

    for p in psutil.process_iter(attrs):
        try:
            info = p.info
            mem_info = info.get("memory_info")
            rss_mb = round(mem_info.rss / 1024 / 1024, 1) if mem_info else 0.0
            procs.append({
                "pid": info["pid"],
                "name": info["name"] or "unknown",
                "username": info["username"] or "unknown",
                "cpu_percent": round(info["cpu_percent"] or 0.0, 1),
                "memory_mb": rss_mb,
                "memory_percent": round(info["memory_percent"] or 0.0, 1),
                "status": info["status"] or "running",
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if sort_by == "memory":
        procs.sort(key=lambda x: x["memory_mb"], reverse=True)
    else:
        procs.sort(key=lambda x: x["cpu_percent"], reverse=True)

    return procs[:limit]


def record_metric_snapshot(db: OrmSession) -> None:
    """Capture a lightweight metric row and prune data older than 7 days."""
    stats = get_live_metrics()

    metric = ServerMetric(
        created_at=utcnow(),
        cpu_percent=stats["cpu"]["percent"],
        memory_percent=stats["memory"]["percent"],
        memory_used_mb=stats["memory"]["used_mb"],
        swap_percent=stats["swap"]["percent"],
        disk_percent=stats["disk"]["percent"],
        load_1m=stats["load"][0],
        load_5m=stats["load"][1],
        load_15m=stats["load"][2],
        net_rx_kb=stats["network"]["rx_kb_s"],
        net_tx_kb=stats["network"]["tx_kb_s"],
    )
    db.add(metric)

    # Prune data older than 7 days
    cutoff = utcnow() - timedelta(days=7)
    db.execute(delete(ServerMetric).where(ServerMetric.created_at < cutoff))
    db.commit()


def get_history(db: OrmSession, hours: int = 24, max_points: int = 60) -> List[Dict]:
    """Fetch historical metric snapshots downsampled cleanly to max_points."""
    cutoff = utcnow() - timedelta(hours=hours)

    rows = db.scalars(
        select(ServerMetric)
        .where(ServerMetric.created_at >= cutoff)
        .order_by(ServerMetric.created_at.asc())
    ).all()

    if not rows:
        return []

    # Downsample if count exceeds max_points
    total_rows = len(rows)
    step = max(1, total_rows // max_points)
    sampled = rows[::step]
    if rows[-1] not in sampled:
        sampled.append(rows[-1])

    return [
        {
            "ts": r.created_at.strftime("%Y-%m-%dT%H:%M:%S"),
            "time": r.created_at.strftime("%H:%M" if hours <= 24 else "%m-%d %H:%M"),
            "cpu": round(r.cpu_percent, 1),
            "memory": round(r.memory_percent, 1),
            "load": round(r.load_1m, 2),
            "net_rx": r.net_rx_kb,
            "net_tx": r.net_tx_kb,
        }
        for r in sampled
    ]


# --------------------------------------------------------------------------
# Background Sampler
# --------------------------------------------------------------------------

import threading

_sampler_stop = threading.Event()
_sampler_thread: Optional[threading.Thread] = None


def _sampler_worker():
    from app.database import session_scope

    while not _sampler_stop.is_set():
        try:
            with session_scope() as db:
                record_metric_snapshot(db)
        except Exception as exc:
            logger.debug("metric snapshot failed: %s", exc)

        # Sleep in short increments so stop() responds quickly
        for _ in range(60):
            if _sampler_stop.is_set():
                break
            time.sleep(1)


def start_metrics_sampler():
    global _sampler_thread, _sampler_stop
    if _sampler_thread and _sampler_thread.is_alive():
        return
    _sampler_stop.clear()
    _sampler_thread = threading.Thread(target=_sampler_worker, daemon=True, name="metrics-sampler")
    _sampler_thread.start()


def stop_metrics_sampler():
    global _sampler_stop, _sampler_thread
    _sampler_stop.set()
    if _sampler_thread and _sampler_thread.is_alive():
        _sampler_thread.join(timeout=2)


# --------------------------------------------------------------------------
# Disk Usage Breakdown & Top Directories
# --------------------------------------------------------------------------

_disk_breakdown_cache: Optional[Dict] = None
_disk_breakdown_time: float = 0.0
_disk_breakdown_lock = threading.Lock()


def get_disk_breakdown(force_refresh: bool = False) -> Dict:
    """Return disk breakdown data, using in-memory cache if fresher than 120s."""
    global _disk_breakdown_cache, _disk_breakdown_time
    now = time.time()
    with _disk_breakdown_lock:
        if not force_refresh and _disk_breakdown_cache and (now - _disk_breakdown_time < 120):
            return _disk_breakdown_cache

        breakdown = _compute_disk_breakdown()
        _disk_breakdown_cache = breakdown
        _disk_breakdown_time = now
        return breakdown


def _compute_disk_breakdown() -> Dict:
    """Scan key server locations and calculate top directories eating space."""
    import os
    from pathlib import Path
    from app.services import sites as sites_service
    from app.services import backup as backup_service
    from app.services import databases as db_service
    from app.services.files import directory_size_bytes, format_size

    try:
        disk = psutil.disk_usage("/")
        total_bytes = disk.total
        used_bytes = disk.used
        free_bytes = disk.free
        percent = disk.percent
    except Exception:
        total_bytes = 0
        used_bytes = 0
        free_bytes = 0
        percent = 0.0

    top_directories = []
    category_totals = {
        "Websites": 0,
        "Databases": 0,
        "Backups": 0,
        "Logs": 0,
        "Package Cache": 0,
        "Temp Files": 0,
    }

    # 1. Websites (/var/www or configured sites_root)
    try:
        sites_root = sites_service.sites_root()
        if sites_root.exists() and sites_root.is_dir():
            for child in sites_root.iterdir():
                try:
                    if child.is_dir() and not child.is_symlink():
                        size = directory_size_bytes(child)
                        category_totals["Websites"] += size
                        if size > 100 * 1024:  # > 100 KB
                            top_directories.append({
                                "path": str(child),
                                "name": child.name,
                                "category": "Websites",
                                "bytes": size,
                                "human_size": format_size(size),
                                "percent_of_used": round(size / used_bytes * 100, 1) if used_bytes else 0.0,
                            })
                except (OSError, PermissionError):
                    continue
    except Exception as exc:
        logger.debug("Failed scanning sites root: %s", exc)

    # 2. Databases (/var/lib/mysql or size_map)
    mysql_path = Path("/var/lib/mysql")
    db_scanned = False
    try:
        if mysql_path.exists() and mysql_path.is_dir():
            mysql_size = directory_size_bytes(mysql_path)
            category_totals["Databases"] = mysql_size
            db_scanned = True
            if mysql_size > 100 * 1024:
                top_directories.append({
                    "path": str(mysql_path),
                    "name": "MariaDB / MySQL Data",
                    "category": "Databases",
                    "bytes": mysql_size,
                    "human_size": format_size(mysql_size),
                    "percent_of_used": round(mysql_size / used_bytes * 100, 1) if used_bytes else 0.0,
                })
    except (OSError, PermissionError):
        pass

    if not db_scanned:
        try:
            if db_service.is_available():
                sizes = db_service.size_map()
                db_total = sum(int(s * 1024 * 1024) for s in sizes.values())
                category_totals["Databases"] = db_total
                for db_name, size_mb in sizes.items():
                    b = int(size_mb * 1024 * 1024)
                    if b > 100 * 1024:
                        top_directories.append({
                            "path": f"/var/lib/mysql/{db_name}",
                            "name": f"DB: {db_name}",
                            "category": "Databases",
                            "bytes": b,
                            "human_size": format_size(b),
                            "percent_of_used": round(b / used_bytes * 100, 1) if used_bytes else 0.0,
                        })
        except Exception as exc:
            logger.debug("Failed scanning database sizes: %s", exc)

    # 3. Backups (/var/backups/lite-panel)
    try:
        backup_root = backup_service.get_backup_root()
        if backup_root.exists() and backup_root.is_dir():
            for child in backup_root.iterdir():
                try:
                    if child.is_dir() and not child.is_symlink():
                        size = directory_size_bytes(child)
                        category_totals["Backups"] += size
                        if size > 100 * 1024:
                            top_directories.append({
                                "path": str(child),
                                "name": f"Backups: {child.name}",
                                "category": "Backups",
                                "bytes": size,
                                "human_size": format_size(size),
                                "percent_of_used": round(size / used_bytes * 100, 1) if used_bytes else 0.0,
                            })
                    elif child.is_file() and not child.is_symlink():
                        size = child.stat().st_size
                        category_totals["Backups"] += size
                except (OSError, PermissionError):
                    continue
    except Exception as exc:
        logger.debug("Failed scanning backup root: %s", exc)

    # 4. Logs (/var/log)
    try:
        log_root = Path("/var/log")
        if log_root.exists() and log_root.is_dir():
            for child in log_root.iterdir():
                try:
                    if child.is_dir() and not child.is_symlink():
                        size = directory_size_bytes(child)
                        category_totals["Logs"] += size
                        if size > 5 * 1024 * 1024:  # > 5 MB
                            top_directories.append({
                                "path": str(child),
                                "name": f"Logs: {child.name}",
                                "category": "Logs",
                                "bytes": size,
                                "human_size": format_size(size),
                                "percent_of_used": round(size / used_bytes * 100, 1) if used_bytes else 0.0,
                            })
                    elif child.is_file() and not child.is_symlink():
                        size = child.stat().st_size
                        category_totals["Logs"] += size
                        if size > 10 * 1024 * 1024:  # > 10 MB file
                            top_directories.append({
                                "path": str(child),
                                "name": f"Log: {child.name}",
                                "category": "Logs",
                                "bytes": size,
                                "human_size": format_size(size),
                                "percent_of_used": round(size / used_bytes * 100, 1) if used_bytes else 0.0,
                            })
                except (OSError, PermissionError):
                    continue
    except Exception as exc:
        logger.debug("Failed scanning log root: %s", exc)

    # 5. Package Cache (/var/cache)
    try:
        cache_root = Path("/var/cache")
        if cache_root.exists() and cache_root.is_dir():
            cache_size = directory_size_bytes(cache_root)
            category_totals["Package Cache"] = cache_size
            if cache_size > 10 * 1024 * 1024:
                top_directories.append({
                    "path": str(cache_root),
                    "name": "APT / Package Cache",
                    "category": "Package Cache",
                    "bytes": cache_size,
                    "human_size": format_size(cache_size),
                    "percent_of_used": round(cache_size / used_bytes * 100, 1) if used_bytes else 0.0,
                })
    except (OSError, PermissionError):
        pass

    # 6. Temp Files (/tmp)
    try:
        tmp_root = Path("/tmp")
        if tmp_root.exists() and tmp_root.is_dir():
            tmp_size = directory_size_bytes(tmp_root)
            category_totals["Temp Files"] = tmp_size
            if tmp_size > 10 * 1024 * 1024:
                top_directories.append({
                    "path": str(tmp_root),
                    "name": "/tmp",
                    "category": "Temp Files",
                    "bytes": tmp_size,
                    "human_size": format_size(tmp_size),
                    "percent_of_used": round(tmp_size / used_bytes * 100, 1) if used_bytes else 0.0,
                })
    except (OSError, PermissionError):
        pass

    # Sort top directories descending by size
    top_directories.sort(key=lambda x: x["bytes"], reverse=True)
    top_directories = top_directories[:15]

    accounted_bytes = sum(category_totals.values())
    system_other = max(0, used_bytes - accounted_bytes)

    categories = [
        {
            "name": "Websites",
            "path": "/var/www",
            "bytes": category_totals["Websites"],
            "human_size": format_size(category_totals["Websites"]),
            "percent": round(category_totals["Websites"] / used_bytes * 100, 1) if used_bytes else 0.0,
        },
        {
            "name": "Databases",
            "path": "/var/lib/mysql",
            "bytes": category_totals["Databases"],
            "human_size": format_size(category_totals["Databases"]),
            "percent": round(category_totals["Databases"] / used_bytes * 100, 1) if used_bytes else 0.0,
        },
        {
            "name": "Backups",
            "path": "/var/backups",
            "bytes": category_totals["Backups"],
            "human_size": format_size(category_totals["Backups"]),
            "percent": round(category_totals["Backups"] / used_bytes * 100, 1) if used_bytes else 0.0,
        },
        {
            "name": "Logs",
            "path": "/var/log",
            "bytes": category_totals["Logs"],
            "human_size": format_size(category_totals["Logs"]),
            "percent": round(category_totals["Logs"] / used_bytes * 100, 1) if used_bytes else 0.0,
        },
        {
            "name": "Cache & Temp",
            "path": "/var/cache, /tmp",
            "bytes": category_totals["Package Cache"] + category_totals["Temp Files"],
            "human_size": format_size(category_totals["Package Cache"] + category_totals["Temp Files"]),
            "percent": round((category_totals["Package Cache"] + category_totals["Temp Files"]) / used_bytes * 100, 1) if used_bytes else 0.0,
        },
        {
            "name": "System & OS",
            "path": "/",
            "bytes": system_other,
            "human_size": format_size(system_other),
            "percent": round(system_other / used_bytes * 100, 1) if used_bytes else 0.0,
        },
    ]

    return {
        "disk": {
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
            "free_bytes": free_bytes,
            "total_human": format_size(total_bytes),
            "used_human": format_size(used_bytes),
            "free_human": format_size(free_bytes),
            "percent": percent,
        },
        "categories": categories,
        "top_directories": top_directories,
        "scanned_at": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
    }


