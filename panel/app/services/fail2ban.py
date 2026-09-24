"""Fail2ban service: install, panel-owned jail config, live status and bans.

The panel owns three files and never touches the packaged ones:

* ``/etc/fail2ban/jail.d/lite-panel.local`` -- the jails, rendered from the
  ``fail2ban_settings`` row.
* ``/etc/fail2ban/fail2ban.d/lite-panel.local`` -- daemon settings.
* ``/etc/fail2ban/filter.d/lite-panel-manual.conf`` -- the filter behind the
  jail that holds permanent bans.

Every write is checked with ``fail2ban-client -t`` before fail2ban restarts, and
the previous files are put back if the check fails, so a bad value can never
leave fail2ban unable to start.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.models import Fail2banPermanentBan, Fail2banSettings
from app.services import renderer
from app.shell import run, stream, which
from app.validators import ValidationError

logger = logging.getLogger(__name__)

FAIL2BAN_DIR = Path("/etc/fail2ban")
MANUAL_JAIL = "lite-panel-manual"
SSHD_JAIL = "sshd"
RECIDIVE_JAIL = "recidive"
ALWAYS_IGNORED = ("127.0.0.1/8", "::1")

MINUTE = 60
HOUR = 3600
DAY = 86400
WEEK = 7 * DAY

# Choices offered in the settings form. Stored values are plain seconds, so a
# value outside these lists (set by an older version, say) still works.
FINDTIME_CHOICES = [(5 * MINUTE, "5 minutes"), (10 * MINUTE, "10 minutes"),
                    (30 * MINUTE, "30 minutes"), (HOUR, "1 hour"), (DAY, "1 day")]
BANTIME_CHOICES = [(10 * MINUTE, "10 minutes"), (30 * MINUTE, "30 minutes"),
                   (HOUR, "1 hour"), (6 * HOUR, "6 hours"), (12 * HOUR, "12 hours"),
                   (DAY, "1 day"), (WEEK, "1 week")]
MAXTIME_CHOICES = [(DAY, "1 day"), (WEEK, "1 week"), (30 * DAY, "30 days"),
                   (365 * DAY, "1 year")]


def _jail_file() -> Path:
    return FAIL2BAN_DIR / "jail.d" / "lite-panel.local"


def _daemon_file() -> Path:
    return FAIL2BAN_DIR / "fail2ban.d" / "lite-panel.local"


def _filter_file() -> Path:
    return FAIL2BAN_DIR / "filter.d" / f"{MANUAL_JAIL}.conf"


def format_duration(seconds: int) -> str:
    """Human label for a duration in seconds: 3600 -> '1 hour'."""
    if seconds < 0:
        return "permanent"
    for unit, name in ((WEEK, "week"), (DAY, "day"), (HOUR, "hour"), (MINUTE, "minute")):
        if seconds >= unit and seconds % unit == 0:
            count = seconds // unit
            return f"{count} {name}{'s' if count != 1 else ''}"
    return f"{seconds} seconds"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def is_installed() -> bool:
    return which("fail2ban-client") is not None


def is_running() -> bool:
    try:
        return run(["fail2ban-client", "ping"], check=False, timeout=10).ok
    except Exception:  # noqa: BLE001 - a status probe must never break the page
        return False


@dataclass
class SshInfo:
    ports: list = field(default_factory=lambda: ["ssh"])
    password_auth: Optional[bool] = None


def parse_sshd_config_dump(output: str) -> SshInfo:
    """Read ports and password auth from ``sshd -T`` output."""
    ports = []
    password_auth = None
    for line in output.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        key, value = parts[0].lower(), parts[1]
        if key == "port" and value.isdigit() and value not in ports:
            ports.append(value)
        elif key == "passwordauthentication":
            password_auth = value.lower() == "yes"
    return SshInfo(ports=ports or ["ssh"], password_auth=password_auth)


def detect_ssh() -> SshInfo:
    """The SSH ports actually configured, read fresh each time so a port change
    made on the SSH side is picked up on the next save."""
    try:
        res = run(["sshd", "-T"], check=False, timeout=10)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sshd -T failed: %s", exc)
        return SshInfo()
    if not res.ok:
        return SshInfo()
    return parse_sshd_config_dump(res.stdout)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def get_settings(db: OrmSession) -> Fail2banSettings:
    """The settings row, created with the recommended defaults on first use."""
    row = db.scalar(select(Fail2banSettings).limit(1))
    if row is None:
        row = Fail2banSettings()
        db.add(row)
        db.commit()
    return row


def validate_ip(value: str, *, allow_network: bool = False) -> str:
    """Normalise an IP (or, when allowed, a CIDR range) or raise ValidationError."""
    value = (value or "").strip()
    if not value:
        raise ValidationError("IP address is required.")
    try:
        if allow_network and "/" in value:
            return str(ipaddress.ip_network(value, strict=False))
        return str(ipaddress.ip_address(value))
    except ValueError:
        kind = "IP address or CIDR range" if allow_network else "IP address"
        raise ValidationError(f"'{value}' is not a valid {kind}.") from None


def parse_ignoreip(text: str) -> list:
    """Split the never-ban list on whitespace/commas and validate each entry."""
    entries = []
    for token in re.split(r"[\s,]+", text or ""):
        if not token:
            continue
        normalised = validate_ip(token, allow_network=True)
        if normalised not in entries:
            entries.append(normalised)
    return entries


def _in_range(name: str, value: int, low: int, high: int, *, duration: bool = True) -> int:
    if not low <= value <= high:
        fmt = format_duration if duration else str
        raise ValidationError(f"{name} must be between {fmt(low)} and {fmt(high)}.")
    return value


def update_settings(
    db: OrmSession,
    *,
    sshd_enabled: bool,
    maxretry: int,
    findtime: int,
    bantime: int,
    increment_enabled: bool,
    increment_maxtime: int,
    recidive_enabled: bool,
    ignoreip: str,
) -> Fail2banSettings:
    """Validate and store new settings. Does not touch the server."""
    maxretry = _in_range("Failed attempts", maxretry, 1, 100, duration=False)
    findtime = _in_range("Time window", findtime, MINUTE, WEEK)
    bantime = _in_range("Ban length", bantime, MINUTE, 365 * DAY)
    increment_maxtime = _in_range("Maximum ban length", increment_maxtime, MINUTE, 365 * DAY)
    if increment_enabled and increment_maxtime < bantime:
        raise ValidationError("Maximum ban length can't be shorter than the first ban length.")
    entries = parse_ignoreip(ignoreip)

    row = get_settings(db)
    row.sshd_enabled = sshd_enabled
    row.maxretry = maxretry
    row.findtime = findtime
    row.bantime = bantime
    row.increment_enabled = increment_enabled
    row.increment_maxtime = increment_maxtime
    row.recidive_enabled = recidive_enabled
    row.ignoreip = "\n".join(entries)
    db.commit()
    return row


def add_ignoreip(db: OrmSession, ip: str) -> bool:
    """Add one address to the never-ban list. Returns False if it was already
    there or isn't a usable IP (a dev box's 'testclient', say)."""
    try:
        ip = validate_ip(ip)
    except ValidationError:
        return False
    row = get_settings(db)
    entries = parse_ignoreip(row.ignoreip)
    if ip in entries:
        return False
    entries.append(ip)
    row.ignoreip = "\n".join(entries)
    db.commit()
    return True


def is_ignored(ip: str, entries: Iterable[str]) -> bool:
    """True if *ip* falls inside any never-ban entry (localhost included)."""
    addr = ipaddress.ip_address(ip)
    for entry in list(ALWAYS_IGNORED) + list(entries):
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Rendering and applying
# ---------------------------------------------------------------------------


def build_context(row: Fail2banSettings, ssh: SshInfo) -> dict:
    ignoreip = list(ALWAYS_IGNORED) + [e for e in parse_ignoreip(row.ignoreip)
                                       if e not in ALWAYS_IGNORED]
    longest = row.increment_maxtime if row.increment_enabled else row.bantime
    return {
        "settings": row,
        "ignoreip": ignoreip,
        "ssh_ports": ssh.ports,
        # Keep history for at least a week (recidive looks back a day and
        # bans for a week) and never less than the longest ban.
        "dbpurgeage": max(longest, WEEK),
    }


def _log(log: Optional[Callable], message: str) -> None:
    logger.info(message)
    if log:
        log(message)


def apply_config(db: OrmSession, log: Optional[Callable] = None, *, force_restart: bool = False) -> None:
    """Render the panel's files, verify them, and restart fail2ban if needed.

    On a failed check the previous files are restored and RuntimeError is
    raised with fail2ban's own explanation. After the restart every enabled
    jail must have a blocking action, or RuntimeError is raised -- a jail that
    detects attackers but blocks nothing is worse than an error.
    """
    row = get_settings(db)
    context = build_context(row, detect_ssh())
    targets = {
        _jail_file(): "fail2ban-jail.local.j2",
        _daemon_file(): "fail2ban.local.j2",
        _filter_file(): "fail2ban-filter-manual.conf.j2",
    }

    previous = {path: (path.read_text(encoding="utf-8") if path.exists() else None)
                for path in targets}
    changed = False
    for path, template in targets.items():
        changed |= renderer.render_to_file(template, path, context)

    check = run(["fail2ban-client", "-t"], check=False, timeout=60)
    if not check.ok:
        for path, content in previous.items():
            if content is None:
                renderer.remove_file(path)
            else:
                renderer.write_atomic(path, content)
        detail = (check.stderr or check.stdout).strip()[-800:]
        raise RuntimeError(f"fail2ban rejected the new configuration, previous settings kept: {detail}")

    # A full restart, never `fail2ban-client reload`: reload can leave a jail
    # with no actions at all when its action changes (seen on Ubuntu 24.04
    # right after install -- the jail kept "banning" while nothing was
    # blocked). Bans survive the restart through fail2ban's own database,
    # and permanent bans are re-applied from ours below.
    if changed or force_restart or not is_running():
        _log(log, "Restarting fail2ban…")
        run(["systemctl", "enable", "fail2ban"], check=False, timeout=30)
        run(["systemctl", "restart", "fail2ban"], check=True, timeout=90)
        if not wait_until_running():
            raise RuntimeError("fail2ban did not come back after restarting. "
                               "Check `journalctl -u fail2ban` on the server.")

    reapply_permanent_bans(db, log)

    broken = jails_without_actions(expected_jails(row))
    if broken:
        raise RuntimeError(f"fail2ban is running but these jails have no blocking action: "
                           f"{', '.join(broken)}. Check /var/log/fail2ban.log on the server.")
    _log(log, "fail2ban is active and blocking.")


def wait_until_running(timeout: float = 30) -> bool:
    """Poll until the daemon answers; restart returns before it's ready."""
    deadline = time.monotonic() + timeout
    while True:
        if is_running():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def expected_jails(row: Fail2banSettings) -> list:
    jails = [MANUAL_JAIL]
    if row.sshd_enabled:
        jails.insert(0, SSHD_JAIL)
    if row.recidive_enabled:
        jails.append(RECIDIVE_JAIL)
    return jails


def parse_jail_actions(output: str) -> list:
    """Parse ``fail2ban-client get <jail> actions``."""
    if "no actions" in output.lower():
        return []
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return [line for line in lines if not line.lower().startswith("the jail")]


def jails_without_actions(jails: Iterable[str]) -> list:
    """Jails that are missing entirely or that detect but can't block."""
    broken = []
    for jail in jails:
        res = run(["fail2ban-client", "get", jail, "actions"], check=False, timeout=15)
        if not res.ok or not parse_jail_actions(res.stdout):
            broken.append(jail)
    return broken


def install(db: OrmSession, log: Callable) -> None:
    log("Updating package lists…")
    run(["apt-get", "update", "-qq"], check=True, timeout=600)

    # python3-systemd lets fail2ban read the journal (backend = systemd).
    log("Installing fail2ban…")
    ret = stream(["apt-get", "install", "-y", "fail2ban", "python3-systemd"], log)
    if ret != 0:
        raise RuntimeError(f"apt-get install fail2ban failed with exit code {ret}")
    if not is_installed():
        raise RuntimeError("Installation appeared to succeed but fail2ban-client was not found.")

    log("Writing lite-panel jail configuration…")
    apply_config(db, log)
    log("fail2ban installed and protecting SSH.")


def uninstall(log: Callable) -> None:
    """Remove fail2ban. Settings and permanent bans stay in the panel's
    database, so a reinstall picks up where it left off."""
    log("Stopping fail2ban…")
    run(["systemctl", "stop", "fail2ban"], check=False, timeout=60)

    log("Purging fail2ban package…")
    ret = stream(["apt-get", "purge", "-y", "fail2ban"], log)
    if ret != 0:
        raise RuntimeError(f"apt-get purge fail2ban failed with exit code {ret}")

    for path in (_jail_file(), _daemon_file(), _filter_file()):
        if renderer.remove_file(path):
            log(f"Removed {path}")
    log("fail2ban has been uninstalled. Existing bans were lifted.")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def parse_jail_status(output: str) -> dict:
    """Parse ``fail2ban-client status <jail>``."""
    result = {"currently_failed": 0, "total_failed": 0,
              "currently_banned": 0, "total_banned": 0, "banned_ips": []}
    keys = {
        "currently failed": "currently_failed",
        "total failed": "total_failed",
        "currently banned": "currently_banned",
        "total banned": "total_banned",
    }
    for line in output.splitlines():
        label, sep, value = line.partition(":")
        if not sep:
            continue
        label = label.strip(" |`-\t").lower()
        value = value.strip()
        if label in keys and value.isdigit():
            result[keys[label]] = int(value)
        elif label == "banned ip list":
            result["banned_ips"] = value.split()
    return result


_WITH_TIME = re.compile(
    r"^(?P<ip>\S+)\s+(?P<start>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s*\+\s*(?P<secs>-?\d+)\s*=\s*(?P<end>.+?)\s*$"
)


def parse_banip_with_time(output: str) -> dict:
    """Parse ``fail2ban-client get <jail> banip --with-time`` into
    ``{ip: {"banned_at", "duration", "expires_at"}}``."""
    bans = {}
    for line in output.splitlines():
        match = _WITH_TIME.match(line.strip())
        if not match:
            continue
        secs = int(match["secs"])
        bans[match["ip"]] = {
            "banned_at": match["start"],
            "duration": secs,
            "expires_at": None if secs < 0 else match["end"],
        }
    return bans


def _jail_bans(jail: str) -> tuple:
    """(status dict, list of ban dicts) for one jail, or (None, []) if the
    jail isn't running."""
    res = run(["fail2ban-client", "status", jail], check=False, timeout=15)
    if not res.ok:
        return None, []
    status = parse_jail_status(res.stdout)

    timed = {}
    res = run(["fail2ban-client", "get", jail, "banip", "--with-time"], check=False, timeout=15)
    if res.ok:
        timed = parse_banip_with_time(res.stdout)

    bans = []
    for ip in status["banned_ips"]:
        info = timed.get(ip, {})
        bans.append({
            "ip": ip,
            "jail": jail,
            "banned_at": info.get("banned_at"),
            "expires_at": info.get("expires_at"),
            "permanent": jail == MANUAL_JAIL or info.get("duration", 0) < 0,
        })
    return status, bans


def get_status() -> dict:
    """Everything the page shows about the running daemon."""
    running = is_running()
    status = {"running": running, "jails": {}, "bans": [],
              "total_failed": 0, "total_banned": 0}
    status["broken_jails"] = []
    if not running:
        return status
    for jail in (SSHD_JAIL, RECIDIVE_JAIL, MANUAL_JAIL):
        try:
            jail_status, bans = _jail_bans(jail)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read fail2ban jail %s: %s", jail, exc)
            continue
        if jail_status is None:
            continue
        status["jails"][jail] = jail_status
        status["bans"].extend(bans)
        status["total_failed"] += jail_status["total_failed"]
        status["total_banned"] += jail_status["total_banned"]
    status["broken_jails"] = jails_without_actions(status["jails"])
    return status


# ---------------------------------------------------------------------------
# Bans
# ---------------------------------------------------------------------------


def ban_ip(db: OrmSession, ip: str, *, permanent: bool, note: str = "",
           protected: Iterable[str] = ()) -> str:
    """Ban *ip* now. Permanent bans block every port and are remembered in the
    panel's database; temporary ones go through the SSH jail and follow its
    ban length (and growing bans)."""
    ip = validate_ip(ip)
    row = get_settings(db)
    if is_ignored(ip, parse_ignoreip(row.ignoreip)):
        raise ValidationError(f"{ip} is on the never-ban list. Remove it from that list first.")
    if ip in set(protected):
        raise ValidationError(f"{ip} is the address you're using the panel from; banning it would lock you out.")

    if permanent:
        if db.scalar(select(Fail2banPermanentBan).where(Fail2banPermanentBan.ip == ip)) is None:
            db.add(Fail2banPermanentBan(ip=ip, note=(note or "").strip()[:255]))
            db.commit()
        run(["fail2ban-client", "set", MANUAL_JAIL, "banip", ip], check=True, timeout=30)
    else:
        if not row.sshd_enabled:
            raise ValidationError("The SSH jail is turned off. Turn it on, or choose a permanent ban.")
        run(["fail2ban-client", "set", SSHD_JAIL, "banip", ip], check=True, timeout=30)
    return ip


def unban_ip(db: OrmSession, ip: str) -> str:
    """Lift every ban on *ip*, in every jail, and forget a permanent ban."""
    ip = validate_ip(ip)
    for row in db.scalars(select(Fail2banPermanentBan).where(Fail2banPermanentBan.ip == ip)).all():
        db.delete(row)
    db.commit()
    run(["fail2ban-client", "unban", ip], check=False, timeout=30)
    return ip


def list_permanent_bans(db: OrmSession) -> list:
    return list(db.scalars(select(Fail2banPermanentBan).order_by(Fail2banPermanentBan.created_at)).all())


def reapply_permanent_bans(db: OrmSession, log: Optional[Callable] = None) -> int:
    """Make sure every permanent ban in the database is active in fail2ban.

    fail2ban normally restores bans from its own database, but not after a
    reinstall; the panel's database is the record that survives."""
    wanted = [row.ip for row in list_permanent_bans(db)]
    if not wanted:
        return 0
    res = run(["fail2ban-client", "status", MANUAL_JAIL], check=False, timeout=15)
    active = set(parse_jail_status(res.stdout)["banned_ips"]) if res.ok else set()
    applied = 0
    for ip in wanted:
        if ip in active:
            continue
        if run(["fail2ban-client", "set", MANUAL_JAIL, "banip", ip], check=False, timeout=30).ok:
            applied += 1
    if applied:
        _log(log, f"Re-applied {applied} permanent ban(s).")
    return applied
