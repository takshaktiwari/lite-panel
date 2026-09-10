"""UFW (Uncomplicated Firewall) service management.

Executes ufw commands using app.shell, parsing status, rules, and managing ports.
Handles non-Linux / development environments gracefully.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.shell import CommandError, run, which
from app.validators import ValidationError

logger = logging.getLogger(__name__)


@dataclass
class FirewallRule:
    number: int
    to_port: str
    action: str
    from_ip: str
    comment: str = ""
    raw: str = ""


@dataclass
class FirewallStatus:
    available: bool
    enabled: bool
    default_incoming: str = "deny"
    default_outgoing: str = "allow"
    rules: List[FirewallRule] = None

    def __post_init__(self):
        if self.rules is None:
            self.rules = []


# Known ports with explanations and recommendations
ESSENTIAL_PRESETS = [
    {
        "name": "SSH",
        "port": "22",
        "proto": "tcp",
        "desc": "Remote command-line access. Required for terminal & remote administration.",
        "level": "critical",
    },
    {
        "name": "HTTP",
        "port": "80",
        "proto": "tcp",
        "desc": "Standard web traffic and Let's Encrypt certificate challenge verification.",
        "level": "essential",
    },
    {
        "name": "HTTPS",
        "port": "443",
        "proto": "tcp",
        "desc": "Encrypted secure web traffic for websites and Lite-Panel web dashboard.",
        "level": "essential",
    },
    {
        "name": "FTP Control",
        "port": "21",
        "proto": "tcp",
        "desc": "FTP connection establishment (if using vsftpd).",
        "level": "optional",
    },
    {
        "name": "FTP Passive Data",
        "port": "40000:40100",
        "proto": "tcp",
        "desc": "Passive port range for FTP file listings and transfers.",
        "level": "optional",
    },
]


def is_installed() -> bool:
    """Check if ufw is installed and available in PATH."""
    return which("ufw") is not None


def get_status() -> FirewallStatus:
    """Query current UFW status and parse numbered rules."""
    if not is_installed():
        return FirewallStatus(available=False, enabled=False)

    try:
        res = run(["ufw", "status", "verbose"], check=False, timeout=10)
    except Exception as exc:
        logger.debug("ufw status error: %s", exc)
        return FirewallStatus(available=True, enabled=False)

    stdout = res.stdout.strip()
    is_active = "Status: active" in stdout

    # Parse defaults e.g. "Default: deny (incoming), allow (outgoing), disabled (routed)"
    default_in = "deny"
    default_out = "allow"
    default_match = re.search(r"Default:\s*(\w+)\s*\(incoming\),\s*(\w+)\s*\(outgoing\)", stdout)
    if default_match:
        default_in = default_match.group(1)
        default_out = default_match.group(2)

    rules: List[FirewallRule] = []
    if is_active:
        try:
            num_res = run(["ufw", "status", "numbered"], check=False, timeout=10)
            rules = parse_numbered_rules(num_res.stdout)
        except Exception as exc:
            logger.debug("error parsing numbered rules: %s", exc)

    return FirewallStatus(
        available=True,
        enabled=is_active,
        default_incoming=default_in,
        default_outgoing=default_out,
        rules=rules,
    )


def parse_numbered_rules(output: str) -> List[FirewallRule]:
    """Parse `ufw status numbered` output.
    Format example:
    [ 1] 22/tcp                     ALLOW IN    Anywhere                   # SSH
    [ 2] 80/tcp                     ALLOW IN    Anywhere
    [ 3] 3306/tcp                   ALLOW IN    203.0.113.50               # Remote DB
    [ 4] 22/tcp (v6)                ALLOW IN    Anywhere (v6)
    """
    rules: List[FirewallRule] = []
    lines = output.splitlines()

    for line in lines:
        line_clean = line.strip()
        match = re.match(r"^\[\s*(\d+)\]\s+(.*?)\s+(ALLOW|DENY|REJECT|LIMIT)(?:\s+IN|\s+OUT)?\s+(.*?)(?:\s+#\s*(.*))?$", line_clean, re.IGNORECASE)
        if match:
            num = int(match.group(1))
            to_port = match.group(2).strip()
            action = match.group(3).upper()
            from_ip = match.group(4).strip()
            comment = (match.group(5) or "").strip()
            rules.append(
                FirewallRule(
                    number=num,
                    to_port=to_port,
                    action=action,
                    from_ip=from_ip,
                    comment=comment,
                    raw=line_clean,
                )
            )
    return rules


def enable_firewall() -> None:
    """Ensure SSH is allowed before enabling to avoid lockout, then enable."""
    if not is_installed():
        raise RuntimeError("UFW is not installed.")

    # Safety check: ensure port 22 or ssh is in rules or allow it first
    status = get_status()
    has_ssh = any("22" in r.to_port or "ssh" in r.to_port.lower() for r in status.rules)
    if not has_ssh:
        logger.info("Automatically allowing SSH (port 22) prior to enabling UFW to prevent lockout")
        run(["ufw", "allow", "22/tcp"], check=True, timeout=15)

    # Force enable non-interactively
    run(["ufw", "--force", "enable"], check=True, timeout=15)
    logger.info("UFW enabled")


def disable_firewall() -> None:
    if not is_installed():
        raise RuntimeError("UFW is not installed.")
    run(["ufw", "disable"], check=True, timeout=15)
    logger.info("UFW disabled")


def validate_port_specification(port: str) -> str:
    """Validate port or port range (e.g. '80', '40000:40100', '22')."""
    port = port.strip()
    if not re.match(r"^\d{1,5}(:\d{1,5})?$", port):
        raise ValidationError(f"Invalid port or port range: '{port}'. Format: 80 or 40000:40100")
    parts = port.split(":")
    for p in parts:
        num = int(p)
        if num < 1 or num > 65535:
            raise ValidationError(f"Port number {num} out of valid range (1-65535)")
    return port


def validate_ip_specification(ip: str) -> str:
    """Validate IP or CIDR (e.g. 'Anywhere', '192.168.1.1', '10.0.0.0/24')."""
    ip = ip.strip()
    if not ip or ip.lower() in ("any", "anywhere"):
        return "any"

    # Match IPv4 or IPv4 CIDR
    if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(/\d{1,2})?$", ip):
        # Allow IPv6 basic pattern
        if ":" not in ip:
            raise ValidationError(f"Invalid IP address or CIDR notation: '{ip}'")
    return ip


def add_rule(
    port: str,
    proto: str = "tcp",
    action: str = "allow",
    from_ip: str = "any",
    comment: str = "",
) -> None:
    """Add a firewall rule."""
    if not is_installed():
        raise RuntimeError("UFW is not installed.")

    action = action.lower()
    if action not in ("allow", "deny", "reject", "limit"):
        raise ValidationError(f"Invalid action: {action}")

    proto = proto.lower()
    if proto not in ("tcp", "udp", "any"):
        raise ValidationError(f"Invalid protocol: {proto}")

    port = validate_port_specification(port)
    from_ip = validate_ip_specification(from_ip)

    # Clean comment
    comment = re.sub(r'[\r\n"\']', '', comment).strip()[:50]

    cmd = ["ufw"]
    if from_ip == "any":
        port_spec = f"{port}/{proto}" if proto != "any" else port
        cmd.extend([action, port_spec])
    else:
        cmd.extend([action, "from", from_ip, "to", "any", "port", port])
        if proto != "any":
            cmd.extend(["proto", proto])

    if comment:
        cmd.extend(["comment", comment])

    run(cmd, check=True, timeout=15)
    logger.info("Added UFW rule: %s", " ".join(cmd))


def delete_rule(rule_number: int) -> None:
    """Delete a rule by its numbered position in `ufw status numbered`."""
    if not is_installed():
        raise RuntimeError("UFW is not installed.")

    if rule_number < 1:
        raise ValidationError("Rule number must be 1 or greater.")

    run(["ufw", "--force", "delete", str(rule_number)], check=True, timeout=15)
    logger.info("Deleted UFW rule #%d", rule_number)
