"""Swap management.

The panel manages exactly one swap file, ``/swapfile`` -- the same file the
installer creates on very small servers -- so it never touches a swap
partition or anything else an operator set up by hand.  Those are still
listed (read-only) so the page tells the whole truth about the machine.

Everything here is parsing and an argument list: the size is an integer that
has been range-checked, the path is a constant, and no value from the
browser ever reaches a command as text.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from app.services import renderer, system, tuning
from app.shell import run, which
from app.validators import ValidationError

logger = logging.getLogger(__name__)

SWAPFILE = Path("/swapfile")
FSTAB = Path("/etc/fstab")
PROC_SWAPS = Path("/proc/swaps")
SWAPPINESS_PROC = Path("/proc/sys/vm/swappiness")
SYSCTL_FILE = Path("/etc/sysctl.d/99-lite-panel-swap.conf")

MIN_SIZE_MB = 256
MAX_SIZE_MB = 16384
# Disk that must stay free after the swap file is written, so a full swap
# file can't be what fills the disk.
DISK_HEADROOM_MB = 1024
DEFAULT_SWAPPINESS = 60

FSTAB_LINE = f"{SWAPFILE} none swap sw 0 0"


@dataclass
class SwapDevice:
    name: str
    kind: str
    size_mb: int
    used_mb: int
    priority: int

    @property
    def managed(self) -> bool:
        return self.name == str(SWAPFILE)


@dataclass
class SwapStatus:
    supported: bool
    devices: List[SwapDevice] = field(default_factory=list)
    swapfile_exists: bool = False
    swapfile_size_mb: int = 0
    in_fstab: bool = False
    swappiness: Optional[int] = None
    ram_mb: int = 0
    free_disk_mb: int = 0
    recommended_mb: int = 0

    @property
    def total_mb(self) -> int:
        return sum(d.size_mb for d in self.devices)

    @property
    def used_mb(self) -> int:
        return sum(d.used_mb for d in self.devices)

    @property
    def managed_active(self) -> bool:
        return any(d.managed for d in self.devices)

    @property
    def other_devices(self) -> List[SwapDevice]:
        return [d for d in self.devices if not d.managed]


# --------------------------------------------------------------------------
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------


def parse_proc_swaps(text: str) -> List[SwapDevice]:
    """Parse ``/proc/swaps`` (sizes are in KiB)."""
    devices = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        name, kind, size, used, priority = parts[:5]
        if not (size.isdigit() and used.isdigit()):
            continue
        try:
            prio = int(priority)
        except ValueError:
            prio = 0
        devices.append(
            SwapDevice(
                name=name,
                kind=kind,
                size_mb=int(size) // 1024,
                used_mb=int(used) // 1024,
                priority=prio,
            )
        )
    return devices


def _is_swapfile_line(line: str) -> bool:
    fields = line.split()
    return len(fields) >= 3 and fields[0] == str(SWAPFILE) and fields[2] == "swap"


def fstab_has_swapfile(text: str) -> bool:
    return any(_is_swapfile_line(line) for line in text.splitlines() if not line.lstrip().startswith("#"))


def fstab_with_swapfile(text: str) -> str:
    """Add the swap entry if it isn't there; never duplicates it."""
    if fstab_has_swapfile(text):
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + FSTAB_LINE + "\n"


def fstab_without_swapfile(text: str) -> str:
    kept = [
        line for line in text.splitlines()
        if line.lstrip().startswith("#") or not _is_swapfile_line(line)
    ]
    return "\n".join(kept) + ("\n" if kept else "")


def validate_size_mb(value) -> int:
    try:
        size = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError("Swap size must be a number of megabytes.") from None
    if not MIN_SIZE_MB <= size <= MAX_SIZE_MB:
        raise ValidationError(f"Swap size must be between {MIN_SIZE_MB} MB and {MAX_SIZE_MB // 1024} GB.")
    return size


def validate_swappiness(value) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError("Swappiness must be a number from 0 to 100.") from None
    if not 0 <= number <= 100:
        raise ValidationError("Swappiness must be a number from 0 to 100.")
    return number


def render_sysctl(swappiness: int) -> str:
    return f"# {renderer.BANNER}\nvm.swappiness={swappiness}\n"


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def is_supported() -> bool:
    """Swap can only be managed on a Linux box with the swap tools present."""
    return PROC_SWAPS.exists() and which("swapon") is not None and which("mkswap") is not None


def get_status() -> SwapStatus:
    ram = system.total_ram_mb()
    disk = system.disk_usage_for("/") or {}
    status = SwapStatus(
        supported=is_supported(),
        ram_mb=ram,
        free_disk_mb=disk.get("free_mb", 0),
        recommended_mb=tuning.swap_recommendation_mb(ram),
    )
    if not PROC_SWAPS.exists():
        return status

    status.devices = parse_proc_swaps(_read(PROC_SWAPS))
    try:
        status.swapfile_exists = SWAPFILE.is_file()
        status.swapfile_size_mb = SWAPFILE.stat().st_size // (1024 * 1024) if status.swapfile_exists else 0
    except OSError:
        pass
    status.in_fstab = fstab_has_swapfile(_read(FSTAB))
    raw = _read(SWAPPINESS_PROC).strip()
    status.swappiness = int(raw) if raw.isdigit() else None
    return status


# --------------------------------------------------------------------------
# Changes (run inside jobs)
# --------------------------------------------------------------------------


def _require_supported() -> None:
    if not is_supported():
        raise ValidationError(
            "Swap can't be managed on this system (it needs Linux with swapon and mkswap)."
        )


def _swapoff(ctx) -> None:
    ctx.log("Turning the swap file off (this moves anything stored in it back into RAM)")
    ctx.check(["swapoff", str(SWAPFILE)], timeout=600)


def _write_file(ctx, size_mb: int) -> None:
    ctx.log(f"Creating a {size_mb} MB swap file at {SWAPFILE}")
    code = ctx.run(["fallocate", "-l", f"{size_mb}M", str(SWAPFILE)], timeout=120)
    if code != 0:
        # Some filesystems (btrfs, some network volumes) can't fallocate a
        # file that swapon will accept; writing real zeros always works.
        ctx.log("fallocate isn't supported here; writing the file with dd instead (slower)")
        SWAPFILE.unlink(missing_ok=True)
        ctx.check(
            ["dd", "if=/dev/zero", f"of={SWAPFILE}", "bs=1M", f"count={size_mb}", "status=none"],
            timeout=1800,
        )
    os.chmod(SWAPFILE, 0o600)
    ctx.check(["mkswap", str(SWAPFILE)], timeout=120)
    ctx.check(["swapon", str(SWAPFILE)], timeout=120)


def _update_fstab(add: bool) -> None:
    current = _read(FSTAB)
    updated = fstab_with_swapfile(current) if add else fstab_without_swapfile(current)
    if updated != current:
        renderer.write_atomic(FSTAB, updated, mode=0o644)


def set_size(ctx, size_mb) -> None:
    """Create the swap file, or replace it with one of a new size."""
    size_mb = validate_size_mb(size_mb)
    _require_supported()

    status = get_status()
    others = status.other_devices
    if others:
        ctx.log(
            "Other swap is already configured ("
            + ", ".join(f"{d.name} {d.size_mb} MB" for d in others)
            + "); the panel's swap file is added alongside it."
        )

    reclaimed = status.swapfile_size_mb if status.swapfile_exists else 0
    needed = size_mb + DISK_HEADROOM_MB - reclaimed
    if status.free_disk_mb and status.free_disk_mb < needed:
        raise ValidationError(
            f"Not enough free disk: {size_mb} MB of swap needs about {needed} MB free "
            f"(including {DISK_HEADROOM_MB} MB headroom); {status.free_disk_mb} MB is free."
        )

    if status.managed_active:
        current = next(d for d in status.devices if d.managed)
        if current.size_mb == size_mb:
            ctx.log(f"The swap file is already {size_mb} MB; nothing to change")
            _update_fstab(True)
            return
        ctx.log(f"Resizing the swap file from {current.size_mb} MB to {size_mb} MB")
        _swapoff(ctx)
    elif status.swapfile_exists:
        ctx.log("Replacing an inactive swap file")

    if SWAPFILE.exists():
        SWAPFILE.unlink()

    _write_file(ctx, size_mb)
    _update_fstab(True)
    ctx.log(f"Swap is on: {size_mb} MB, and it will come back after a reboot")


def remove(ctx) -> None:
    """Turn off and delete the panel's swap file."""
    _require_supported()
    status = get_status()

    if status.managed_active:
        _swapoff(ctx)
    elif not status.swapfile_exists and not status.in_fstab:
        ctx.log("There is no panel-managed swap file to remove")
        return

    if SWAPFILE.exists():
        SWAPFILE.unlink()
        ctx.log(f"Deleted {SWAPFILE}")
    _update_fstab(False)
    ctx.log("Removed the swap entry from /etc/fstab")
    ctx.log("Swap file removed")


def set_swappiness(ctx, value) -> None:
    """How eagerly the kernel uses swap (0 = only when forced, 100 = eagerly)."""
    number = validate_swappiness(value)
    _require_supported()
    ctx.log(f"Setting vm.swappiness to {number}")
    ctx.check(["sysctl", "-w", f"vm.swappiness={number}"], timeout=30)
    renderer.write_atomic(SYSCTL_FILE, render_sysctl(number))
    ctx.log(f"Saved to {SYSCTL_FILE} so it persists after a reboot")
