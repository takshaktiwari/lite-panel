"""Server facts: resource usage, service state, and the machine's identity.

Everything here degrades gracefully when a fact is unavailable -- the panel
runs on a workstation during development, where there is no systemd and no
``/etc/os-release``, and a dashboard that raised in that situation would make
local development impossible.
"""

from __future__ import annotations

import logging
import platform
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import psutil

from app.shell import CommandError, run, which

logger = logging.getLogger(__name__)

# Services the panel expects to manage once the stack is provisioned.
KNOWN_SERVICES = ("nginx", "mariadb", "vsftpd", "lite-panel")


@dataclass
class ResourceUsage:
    total_mb: int
    used_mb: int

    @property
    def percent(self) -> float:
        return round(self.used_mb / self.total_mb * 100, 1) if self.total_mb else 0.0


@dataclass
class ServiceState:
    name: str
    installed: bool
    active: bool
    detail: str = ""


@dataclass
class SystemInfo:
    hostname: str
    os_name: str
    kernel: str
    uptime_seconds: int
    cpu_count: int
    cpu_percent: float
    memory: ResourceUsage
    swap: ResourceUsage
    disk: ResourceUsage
    load_average: tuple = field(default_factory=lambda: (0.0, 0.0, 0.0))


def total_ram_mb() -> int:
    """Total RAM, the input to every tuning calculation."""
    return int(psutil.virtual_memory().total / (1024 * 1024))


def collect() -> SystemInfo:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")

    try:
        load_average = tuple(round(v, 2) for v in psutil.getloadavg())
    except (AttributeError, OSError):
        load_average = (0.0, 0.0, 0.0)

    try:
        uptime = int(psutil.time.time() - psutil.boot_time())
    except Exception:  # noqa: BLE001
        uptime = 0

    return SystemInfo(
        hostname=socket.gethostname(),
        os_name=os_release_name(),
        kernel=platform.release(),
        uptime_seconds=uptime,
        cpu_count=psutil.cpu_count(logical=True) or 1,
        # A zero interval reports usage since the last call rather than
        # blocking the request for a sampling window.
        cpu_percent=psutil.cpu_percent(interval=0.0),
        memory=ResourceUsage(
            total_mb=int(memory.total / 1024 / 1024),
            used_mb=int((memory.total - memory.available) / 1024 / 1024),
        ),
        swap=ResourceUsage(
            total_mb=int(swap.total / 1024 / 1024),
            used_mb=int(swap.used / 1024 / 1024),
        ),
        disk=ResourceUsage(
            total_mb=int(disk.total / 1024 / 1024),
            used_mb=int(disk.used / 1024 / 1024),
        ),
        load_average=load_average,
    )


def os_release_name() -> str:
    """Pretty OS name from /etc/os-release, falling back to the platform."""
    path = Path("/etc/os-release")
    if path.is_file():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
    return f"{platform.system()} {platform.release()}"


def os_release_id() -> str:
    """``ubuntu`` / ``debian``, or empty where there is no os-release."""
    path = Path("/etc/os-release")
    if path.is_file():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("ID="):
                    return line.split("=", 1)[1].strip().strip('"').lower()
        except OSError:
            pass
    return ""


def is_supported_os() -> bool:
    return os_release_id() in {"ubuntu", "debian"}


def service_state(name: str) -> ServiceState:
    """Whether a systemd unit exists and is running."""
    if not which("systemctl"):
        return ServiceState(name=name, installed=False, active=False, detail="no systemd")

    try:
        result = run(["systemctl", "is-active", name], check=False, timeout=10)
    except (CommandError, OSError) as exc:
        logger.debug("could not query %s: %s", name, exc)
        return ServiceState(name=name, installed=False, active=False, detail="unknown")

    state = (result.stdout or result.stderr or "").strip()
    # "inactive" means the unit exists but is stopped; "unknown"/"failed to
    # get" means it was never installed. The distinction matters on the
    # dashboard: one needs starting, the other needs installing.
    installed = state not in ("", "unknown") and "not-found" not in state
    return ServiceState(name=name, installed=installed, active=state == "active", detail=state)


def service_states(names=KNOWN_SERVICES) -> List[ServiceState]:
    return [service_state(name) for name in names]


def public_ip() -> Optional[str]:
    """The address the panel is reached on.

    Used for the login URL shown after install and for vsftpd's passive mode.
    Queried from the instance metadata service first (instant and offline on
    EC2), falling back to an external lookup.
    """
    try:
        result = run(
            [
                "curl", "-4", "-s", "--max-time", "2",
                "http://169.254.169.254/latest/meta-data/public-ipv4",
            ],
            check=False,
            timeout=5,
        )
        candidate = result.stdout.strip()
        if candidate and _looks_like_ipv4(candidate):
            return candidate
    except Exception:  # noqa: BLE001 - not on EC2, or no metadata service
        pass

    try:
        result = run(["curl", "-4", "-s", "--max-time", "5", "https://ifconfig.me"], check=False)
        candidate = result.stdout.strip()
        if candidate and _looks_like_ipv4(candidate):
            return candidate
    except Exception:  # noqa: BLE001
        pass
    return None


def _looks_like_ipv4(value: str) -> bool:
    try:
        socket.inet_aton(value)
        return value.count(".") == 3
    except OSError:
        return False


def disk_usage_for(path) -> Optional[Dict[str, int]]:
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:
        return None
    return {
        "total_mb": int(usage.total / 1024 / 1024),
        "used_mb": int(usage.used / 1024 / 1024),
        "free_mb": int(usage.free / 1024 / 1024),
    }


def format_uptime(seconds: int) -> str:
    if seconds <= 0:
        return "unknown"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
