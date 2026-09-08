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

