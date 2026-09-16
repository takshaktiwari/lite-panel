"""Security service: LMD (maldet) malware scanner and rkhunter rootkit scanner.

Neither tool is installed automatically. Each is opt-in via the Security page.
Scans run as background jobs so output streams live to the job detail page.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.models import SecurityScanSchedule

from app.shell import run, stream

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MALDET_BIN = "/usr/local/sbin/maldet"
MALDET_INTERNAL_BIN = "/usr/local/maldetect/maldet"
RKHUNTER_BIN = "/usr/bin/rkhunter"
MALDET_QUARANTINE_DIR = Path("/usr/local/maldetect/quarantine")
MALDET_SESS_DIR = Path("/usr/local/maldetect/sess")
MALDET_TMP_DIR = Path("/usr/local/maldetect/tmp")
MALDET_LAST_SCAN = Path("/usr/local/maldetect/sess/session.last")
RKHUNTER_LOG = Path("/var/log/rkhunter.log")
SITES_DIR = "/var/www"


# ---------------------------------------------------------------------------
# Install detection
# ---------------------------------------------------------------------------


def get_maldet_bin() -> str:
    """Return the path to the maldet executable."""
    for path in (MALDET_BIN, MALDET_INTERNAL_BIN, shutil.which("maldet")):
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return MALDET_BIN


def is_maldet_installed() -> bool:
    """Return True if maldet binary exists on disk."""
    return (
        os.path.isfile(MALDET_BIN)
        or os.path.isfile(MALDET_INTERNAL_BIN)
        or shutil.which("maldet") is not None
    )


def is_rkhunter_installed() -> bool:
    """Return True if rkhunter binary exists on disk."""
    return os.path.isfile(RKHUNTER_BIN) or shutil.which("rkhunter") is not None


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def install_maldet(log) -> None:
    """Download and install Linux Malware Detect (LMD / maldet).

    Args:
        log: callable that accepts a string — used to stream output to the job log.
    """
    log("Updating package lists…")
    run(["apt-get", "update", "-qq"], check=True)

    # Ensure /usr/local/sbin exists
    try:
        Path("/usr/local/sbin").mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # maldet is not in apt — install from rfxn.com tarball
    log("Downloading LMD installer from rfxn.com…")
    tmp_dir = Path("/tmp/maldet-install")
    if tmp_dir.exists():
        shutil.rmtree(str(tmp_dir), ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        tarball = tmp_dir / "maldetect-current.tar.gz"

        res = run(
            ["curl", "-fsSL", "-o", str(tarball),
             "https://www.rfxn.com/downloads/maldetect-current.tar.gz"],
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(f"Download failed: {res.stderr}")

        log("Extracting…")
        run(["tar", "-xzf", str(tarball), "-C", str(tmp_dir)], check=True)

        # Find the extracted directory
        dirs = [d for d in tmp_dir.iterdir() if d.is_dir() and d.name.startswith("maldetect")]
        if not dirs:
            raise RuntimeError("Could not find extracted maldetect directory.")
        install_dir = dirs[0]

        log(f"Running installer from {install_dir}…")
        # IMPORTANT: install.sh requires cwd=install_dir because it references ./files/*
        ret = stream(["bash", "install.sh"], log, cwd=str(install_dir))
        if ret != 0:
            raise RuntimeError(f"Installer failed with exit code {ret}")
    finally:
        log("Cleaning up temporary installer files…")
        shutil.rmtree(str(tmp_dir), ignore_errors=True)

    # Ensure symlink /usr/local/sbin/maldet exists if internal binary exists
    if os.path.isfile(MALDET_INTERNAL_BIN) and not os.path.isfile(MALDET_BIN):
        try:
            if os.path.islink(MALDET_BIN):
                os.unlink(MALDET_BIN)
            os.symlink(MALDET_INTERNAL_BIN, MALDET_BIN)
        except Exception as exc:
            log(f"Warning: could not create symlink {MALDET_BIN}: {exc}")

    if not is_maldet_installed():
        raise RuntimeError("Installation appeared to succeed but maldet binary not found.")
    log("LMD (maldet) installed successfully.")


def install_rkhunter(log) -> None:
    """Install rkhunter via apt.

    Args:
        log: callable that accepts a string — used to stream output to the job log.
    """
    log("Updating package lists…")
    run(["apt-get", "update", "-qq"], check=True)

    log("Installing rkhunter…")
    ret = stream(
        ["apt-get", "install", "-y", "rkhunter"],
        log,
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
    )
    if ret != 0:
        raise RuntimeError(f"apt install rkhunter failed with exit code {ret}")

    ensure_rkhunter_configured()

    log("Updating rkhunter data files…")
    stream(["rkhunter", "--update", "--nocolors"], log)
    stream(["rkhunter", "--propupd", "--nocolors"], log)

    if not is_rkhunter_installed():
        raise RuntimeError("Installation appeared to succeed but rkhunter binary not found.")
    log("rkhunter installed successfully.")


# ---------------------------------------------------------------------------
# Maldet scan
# ---------------------------------------------------------------------------


def run_maldet_scan(path: str, log) -> dict[str, Any]:
    """Run a maldet scan on *path* and return a parsed result dict.

    Args:
        path: Filesystem path to scan (e.g. '/var/www' or '/var/www/example.com').
        log: callable that accepts a string — used to stream output to the job log.

    Returns:
        dict with keys: path, scan_id, total_files, hits, hit_list
    """
    if not is_maldet_installed():
        raise RuntimeError("maldet is not installed.")

    log(f"Starting maldet scan on {path} …")
    bin_path = get_maldet_bin()
    output_lines: list[str] = []

    def on_line(line: str) -> None:
        output_lines.append(line)
        log(line)

    stream([bin_path, "-a", path], on_line)
    output = "\n".join(output_lines)
    return _parse_maldet_output(output, path)


def _parse_maldet_output(output: str, path: str) -> dict[str, Any]:
    """Parse maldet stdout into a structured result."""
    hits: list[dict] = []
    scan_id = ""
    total_files = 0
    total_hits = 0

    for line in output.splitlines():
        # Scan ID: "scan report saved, to view run: maldet --report 260916-0810.12345"
        m = re.search(r"maldet\s+--report\s+([0-9a-zA-Z._-]+)", line)
        if m and not scan_id:
            scan_id = m.group(1).strip()

        # Scan ID: "SCAN ID: 260916-0810.12345"
        m = re.search(r"SCAN ID:\s*([0-9a-zA-Z._-]+)", line, re.IGNORECASE)
        if m and not scan_id:
            scan_id = m.group(1).strip()

        # Scan ID: "maldet(12345): {scan}"
        m = re.search(r"maldet\((\d+)\)", line)
        if m and not scan_id:
            scan_id = m.group(1).strip()

        # Summary line: "scan completed on /var/www: files 123, malware hits 2, cleaned hits 0"
        m = re.search(r"files\s+(\d+),\s*malware\s+hits\s+(\d+)", line, re.IGNORECASE)
        if m:
            total_files = int(m.group(1))
            total_hits = int(m.group(2))

        # Total files fallback: "total files scanned: 1234"
        m = re.search(r"total files (?:scanned)?:\s*(\d+)", line, re.IGNORECASE)
        if m and total_files == 0:
            total_files = int(m.group(1))

        # Total hits fallback: "total hits found: 0"
        m = re.search(r"total hits (?:found)?:\s*(\d+)", line, re.IGNORECASE)
        if m and total_hits == 0:
            total_hits = int(m.group(1))

        # Hit line format: "ALERT: <name> : <filepath>"
        m = re.match(r"^\{?\s*ALERT\s*\}?.*?:\s*(.+?)\s*:\s*(.+)$", line, re.IGNORECASE)
        if m:
            hits.append({"threat": m.group(1).strip(), "path": m.group(2).strip(), "severity": "high"})

    # If scan_id was not in stdout, read session.last
    if not scan_id and MALDET_LAST_SCAN.exists():
        try:
            scan_id = MALDET_LAST_SCAN.read_text().strip()
        except Exception:
            pass

    # Read maldet report file for hits if stdout parsing found none
    if not hits and scan_id:
        hits = _read_maldet_report_hits(scan_id)

    if total_hits > len(hits) and scan_id:
        extra_hits = _read_maldet_report_hits(scan_id)
        if extra_hits:
            hits = extra_hits

    return {
        "path": path,
        "scan_id": scan_id,
        "total_files": total_files,
        "hits": len(hits) if hits else total_hits,
        "hit_list": hits,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


def _read_maldet_report_hits(scan_id: str) -> list[dict]:
    """Read hits from maldet's session file or hits file for a given scan_id."""
    hits = []
    # 1. Check session.hits.<scan_id>
    hits_file = MALDET_SESS_DIR / f"session.hits.{scan_id}"
    if hits_file.exists():
        for line in hits_file.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^\{?(.*?)\}?\s*:\s*(/.+)$", line)
            if m:
                hits.append({
                    "threat": m.group(1).strip(),
                    "path": m.group(2).strip(),
                    "severity": "high",
                })
        if hits:
            return hits

    # 2. Check session.<scan_id> in MALDET_SESS_DIR and fallback to MALDET_TMP_DIR
    candidates = []
    if MALDET_SESS_DIR.exists():
        candidates.extend(MALDET_SESS_DIR.glob(f"session.*{scan_id}*"))
    if MALDET_TMP_DIR.exists():
        candidates.extend(MALDET_TMP_DIR.glob(f"*{scan_id}*"))

    for report_path in candidates:
        try:
            text = report_path.read_text(errors="replace")
            in_hit_list = False
            for line in text.splitlines():
                if "FILE HIT LIST:" in line:
                    in_hit_list = True
                    continue
                if in_hit_list:
                    if line.startswith("===") or "Linux Malware Detect" in line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    m = re.match(r"^\s*\{?(.*?)\}?\s*:\s*(/.+)$", line)
                    if m and "/" in m.group(2):
                        hits.append({
                            "threat": m.group(1).strip(),
                            "path": m.group(2).strip(),
                            "severity": "high",
                        })
                else:
                    m = re.match(r"^\s*\{?(.*?)\}?\s*:\s*(/.+)$", line)
                    if m and "/" in m.group(2) and not line.startswith("PATH:") and not line.startswith("HOST:"):
                        hits.append({
                            "threat": m.group(1).strip(),
                            "path": m.group(2).strip(),
                            "severity": "high",
                        })
            if hits:
                break
        except Exception as exc:
            logger.warning("Could not read maldet report %s: %s", report_path, exc)

    return hits


def get_maldet_scan_history() -> list[dict]:
    """List recent maldet scan report IDs and metadata."""
    candidates = []

    # Check MALDET_SESS_DIR (/usr/local/maldetect/sess/)
    if MALDET_SESS_DIR.exists():
        for f in MALDET_SESS_DIR.iterdir():
            if f.is_file() and f.name.startswith("session."):
                suffix = f.name[len("session."):]
                if suffix in ("last", "monitor.current") or suffix.startswith(("hits.", "clean.", "suspend.", "monitor.")):
                    continue
                candidates.append(f)

    # Check MALDET_TMP_DIR (/usr/local/maldetect/tmp/) as fallback
    if MALDET_TMP_DIR.exists():
        for f in MALDET_TMP_DIR.iterdir():
            if f.is_file() and f.name.startswith("report."):
                candidates.append(f)

    if not candidates:
        return []

    reports = []
    for f in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
        scan_id = f.name.split(".", 1)[1] if "." in f.name else f.name
        path_scanned = "/var/www"
        total_files = 0
        hits_count = 0

        try:
            content = f.read_text(errors="replace")
            for line in content.splitlines():
                if line.startswith("SCAN ID:"):
                    sid = line.split("SCAN ID:", 1)[1].strip()
                    if sid:
                        scan_id = sid
                elif line.startswith("PATH:"):
                    path_scanned = line.split("PATH:", 1)[1].strip()
                elif line.startswith("TOTAL FILES:"):
                    try:
                        total_files = int(line.split("TOTAL FILES:", 1)[1].strip())
                    except Exception:
                        pass
                elif line.startswith("TOTAL HITS:"):
                    try:
                        hits_count = int(line.split("TOTAL HITS:", 1)[1].strip())
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("Could not parse maldet session file %s: %s", f, exc)

        if hits_count == 0:
            hits_count = len(_read_maldet_report_hits(scan_id))

        reports.append({
            "scan_id": scan_id,
            "path": path_scanned,
            "total_files": total_files,
            "scanned_at": mtime.isoformat(),
            "hits": hits_count,
        })
        if len(reports) >= 15:
            break

    return reports


def get_maldet_report(scan_id: str) -> dict[str, Any]:
    """Return report details for a specific scan_id."""
    raw_text = ""
    sess_file = MALDET_SESS_DIR / f"session.{scan_id}"
    if sess_file.exists():
        raw_text = sess_file.read_text(errors="replace")
    elif MALDET_SESS_DIR.exists():
        for cand in MALDET_SESS_DIR.glob(f"session.*{scan_id}*"):
            raw_text = cand.read_text(errors="replace")
            break

    if not raw_text and MALDET_TMP_DIR.exists():
        for cand in MALDET_TMP_DIR.glob(f"*{scan_id}*"):
            raw_text = cand.read_text(errors="replace")
            break

    hits = _read_maldet_report_hits(scan_id)
    return {
        "scan_id": scan_id,
        "raw": raw_text,
        "hits": len(hits),
        "hit_list": hits,
    }


# ---------------------------------------------------------------------------
# Quarantine / delete / restore
# ---------------------------------------------------------------------------


def quarantine_file(file_path: str, log) -> None:
    """Quarantine a file using maldet's built-in quarantine."""
    if not is_maldet_installed():
        raise RuntimeError("maldet is not installed.")
    if not os.path.exists(file_path):
        raise ValueError(f"File not found: {file_path}")

    log(f"Quarantining {file_path}…")
    bin_path = get_maldet_bin()
    res = run([bin_path, "-q", file_path], check=False)
    output = (res.stdout or "") + (res.stderr or "")
    for line in output.splitlines():
        log(line)

    if res.returncode != 0:
        raise RuntimeError(f"Quarantine failed: {res.stderr}")
    log("File quarantined.")


def delete_file(file_path: str) -> None:
    """Permanently delete a file flagged as malware."""
    if not os.path.exists(file_path):
        raise ValueError(f"File not found: {file_path}")
    os.remove(file_path)
    logger.info("Deleted flagged file: %s", file_path)


def get_quarantine_list() -> list[dict]:
    """List files currently in maldet's quarantine directory."""
    if not MALDET_QUARANTINE_DIR.exists():
        return []

    items = []
    for f in sorted(MALDET_QUARANTINE_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.is_file():
            mtime = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
            items.append({
                "name": f.name,
                "quarantine_path": str(f),
                "quarantined_at": mtime.isoformat(),
                "size": f.stat().st_size,
            })

    return items


def restore_quarantine(filename: str, log) -> None:
    """Restore a quarantined file using maldet --restore."""
    if not is_maldet_installed():
        raise RuntimeError("maldet is not installed.")

    qpath = MALDET_QUARANTINE_DIR / filename
    if not qpath.exists():
        raise ValueError(f"Quarantined file not found: {filename}")

    log(f"Restoring {filename} from quarantine…")
    bin_path = get_maldet_bin()
    res = run([bin_path, "--restore", str(qpath)], check=False)
    output = (res.stdout or "") + (res.stderr or "")
    for line in output.splitlines():
        log(line)

    if res.returncode != 0:
        raise RuntimeError(f"Restore failed: {res.stderr}")
    log("File restored.")


def delete_quarantine_file(filename: str) -> None:
    """Permanently delete a file from the quarantine directory."""
    qpath = MALDET_QUARANTINE_DIR / filename
    if not qpath.exists():
        raise ValueError(f"Quarantined file not found: {filename}")
    qpath.unlink()
    logger.info("Permanently deleted quarantined file: %s", filename)


# ---------------------------------------------------------------------------
# rkhunter scan
# ---------------------------------------------------------------------------


def ensure_rkhunter_configured() -> None:
    """Ensure /etc/rkhunter.conf.local whitelists known standard Ubuntu system files."""
    conf_dir = Path("/etc")
    if not conf_dir.exists():
        return
    conf_local = conf_dir / "rkhunter.conf.local"
    try:
        content = (
            "# Managed by LitePanel - Whitelist benign system files\n"
            "ALLOW_SSH_ROOT_USER=yes\n"
            "ALLOWHIDDENFILE=/etc/.resolv.conf.systemd-resolved.bak\n"
            "ALLOWHIDDENFILE=/etc/.updated\n"
            "ALLOWDEVFILE=/dev/shm/*\n"
            "ALLOWDEVFILE=/dev/.udev*\n"
        )
        if not conf_local.exists() or "ALLOW_SSH_ROOT_USER" not in conf_local.read_text(errors="replace"):
            conf_local.write_text(content)
    except Exception as exc:
        logger.warning("Could not write %s: %s", conf_local, exc)


def run_rkhunter_scan(log) -> dict[str, Any]:
    """Run rkhunter and return a parsed result dict.

    Args:
        log: callable for streaming output to the job log.

    Returns:
        dict with keys: warnings, warning_list, scanned_at
    """
    if not is_rkhunter_installed():
        raise RuntimeError("rkhunter is not installed.")

    ensure_rkhunter_configured()

    log("Updating rkhunter properties baseline…")
    stream(["rkhunter", "--propupd", "--nocolors"], log)

    log("Updating rkhunter data files…")
    stream(["rkhunter", "--update", "--nocolors"], log)

    log("Running rkhunter system check…")
    output_lines: list[str] = []

    def on_line(line: str) -> None:
        output_lines.append(line)
        log(line)

    stream(
        ["rkhunter", "--check", "--skip-keypress", "--nocolors", "--report-warnings-only"],
        on_line,
    )
    output = "\n".join(output_lines)
    return _parse_rkhunter_output(output)


# Patterns that represent normal operating system noise / benign Debian & Ubuntu artifacts
_RKHUNTER_BENIGN_PATTERNS = [
    re.compile(r"Checking\s+.*\s+\[\s*Warning\s*\]", re.IGNORECASE),  # Test header lines
    re.compile(r"Warning:\s*The SSH and rkhunter configuration options should be the same", re.IGNORECASE),
    re.compile(r"Warning:\s*Suspicious file types found in /dev:\s*$", re.IGNORECASE),
    re.compile(r"Hidden file found:\s*/etc/\.resolv\.conf", re.IGNORECASE),
    re.compile(r"Hidden file found:\s*/etc/\.updated", re.IGNORECASE),
    re.compile(r"User '(?:postfix|postdrop|www-data)' has been added", re.IGNORECASE),
    re.compile(r"Group '(?:postfix|postdrop|www-data)' has been added", re.IGNORECASE),
]


def _parse_rkhunter_output(output: str) -> dict[str, Any]:
    """Parse rkhunter output into structured warnings, filtering out known false positives."""
    warnings: list[dict] = []

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue

        clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()

        # Skip informational banners, test names, and clean summaries
        if "Running Rootkit Hunter" in clean or "Info:" in clean or "Starting test name" in clean:
            continue
        if re.search(r"Rootkits checked\s*:", clean, re.IGNORECASE):
            continue
        if re.search(r"Possible rootkits\s*:\s*0", clean, re.IGNORECASE):
            continue
        if re.search(r"Suspect files\s*:\s*0", clean, re.IGNORECASE):
            continue

        # Skip known benign Ubuntu/Debian operational artifacts
        if any(pat.search(clean) for pat in _RKHUNTER_BENIGN_PATTERNS):
            continue

        # Actual infections / critical detections
        if "[ Infected ]" in clean or "[ infected ]" in clean.lower() or "rootkit found" in clean.lower():
            warnings.append({"message": clean, "severity": "critical"})
        # Actual warnings & suspicious checks
        elif "[ Warning ]" in clean or "Warning:" in clean or "[ warning ]" in clean.lower():
            warnings.append({"message": clean, "severity": "warning"})
        elif "[ Suspect ]" in clean or "[ suspect ]" in clean.lower() or "possible rootkit:" in clean.lower():
            warnings.append({"message": clean, "severity": "warning"})

    return {
        "warnings": len(warnings),
        "warning_list": warnings,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


def get_rkhunter_last_report() -> dict[str, Any] | None:
    """Read and parse the last rkhunter log file."""
    if not RKHUNTER_LOG.exists():
        return None

    try:
        text = RKHUNTER_LOG.read_text(errors="replace")
    except Exception:
        return None

    result = _parse_rkhunter_output(text)
    mtime = datetime.fromtimestamp(RKHUNTER_LOG.stat().st_mtime, timezone.utc)
    result["scanned_at"] = mtime.isoformat()
    return result


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def get_sites() -> list[str]:
    """Return a list of site directory names under /var/www."""
    base = Path(SITES_DIR)
    if not base.exists():
        return []
    return sorted(d.name for d in base.iterdir() if d.is_dir() and not d.name.startswith("."))


# ---------------------------------------------------------------------------
# Scan Scheduling
# ---------------------------------------------------------------------------

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def describe_scan_schedule(schedule: SecurityScanSchedule) -> str:
    """Format human-readable schedule description."""
    freq = schedule.frequency
    h = f"{schedule.hour:02d}:{schedule.minute:02d}"

    if freq == "weekly":
        day_name = WEEKDAY_NAMES[schedule.day_of_week % 7]
        return f"Weekly on {day_name} at {h} UTC"
    if freq == "monthly":
        d = schedule.day_of_month
        suffix = "th"
        if d in [1, 21, 31]:
            suffix = "st"
        elif d in [2, 22]:
            suffix = "nd"
        elif d in [3, 23]:
            suffix = "rd"
        return f"Monthly on day {d}{suffix} at {h} UTC"
    return f"Daily at {h} UTC"


def get_next_scan_run(schedule: SecurityScanSchedule, from_time: Optional[datetime] = None) -> datetime:
    """Calculate the next datetime (UTC) the schedule is scheduled to run."""
    now = from_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    freq = schedule.frequency
    h = schedule.hour
    m = schedule.minute

    if freq == "daily":
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if freq == "weekly":
        days_ahead = (schedule.day_of_week - now.weekday()) % 7
        candidate = (now + timedelta(days=days_ahead)).replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    if freq == "monthly":
        target_day = min(schedule.day_of_month, 28)
        try:
            candidate = now.replace(day=target_day, hour=h, minute=m, second=0, microsecond=0)
        except ValueError:
            candidate = now.replace(day=28, hour=h, minute=m, second=0, microsecond=0)

        if candidate <= now:
            month = now.month + 1
            year = now.year
            if month > 12:
                month = 1
                year += 1
            candidate = candidate.replace(year=year, month=month)
        return candidate

    return now + timedelta(days=1)


def check_scan_schedule_due(schedule: SecurityScanSchedule, now: Optional[datetime] = None) -> bool:
    """Return True if the scan schedule is currently due to run."""
    if not schedule.is_enabled:
        return False

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    last_run = schedule.last_run_at
    if last_run and last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)

    freq = schedule.frequency
    h = schedule.hour
    m = schedule.minute

    if freq == "daily":
        target = current.replace(hour=h, minute=m, second=0, microsecond=0)
        if current >= target:
            return last_run is None or last_run < target
        return False

    if freq == "weekly":
        if current.weekday() == (schedule.day_of_week % 7):
            target = current.replace(hour=h, minute=m, second=0, microsecond=0)
            if current >= target:
                return last_run is None or last_run < target
        return False

    if freq == "monthly":
        target_day = min(schedule.day_of_month, 28)
        if current.day == target_day:
            target = current.replace(hour=h, minute=m, second=0, microsecond=0)
            if current >= target:
                return last_run is None or last_run < target
        return False

    return False


def get_scan_schedules(db: OrmSession) -> list[dict]:
    """List all configured scan schedules with metadata and next run time."""
    schedules = db.scalars(
        select(SecurityScanSchedule).order_by(SecurityScanSchedule.created_at.desc())
    ).all()

    now = datetime.now(timezone.utc)
    result = []
    for s in schedules:
        next_run = get_next_scan_run(s, now)
        result.append({
            "id": s.id,
            "scan_type": s.scan_type,
            "target_path": s.target_path,
            "frequency": s.frequency,
            "hour": s.hour,
            "minute": s.minute,
            "day_of_week": s.day_of_week,
            "day_of_month": s.day_of_month,
            "is_enabled": s.is_enabled,
            "description": describe_scan_schedule(s),
            "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
            "next_run_at": next_run.isoformat(),
        })
    return result


def create_scan_schedule(
    db: OrmSession,
    *,
    scan_type: str = "maldet",
    target_path: str = "/var/www",
    frequency: str = "daily",
    hour: int = 2,
    minute: int = 0,
    day_of_week: int = 0,
    day_of_month: int = 1,
) -> SecurityScanSchedule:
    """Create and persist a new scan schedule."""
    if scan_type not in ("maldet", "rkhunter"):
        raise ValueError(f"Invalid scan type: {scan_type}")

    clean_path = target_path.strip() if scan_type == "maldet" else "/"
    if not clean_path:
        clean_path = "/var/www"

    schedule = SecurityScanSchedule(
        scan_type=scan_type,
        target_path=clean_path,
        frequency=frequency if frequency in ("daily", "weekly", "monthly") else "daily",
        hour=max(0, min(23, int(hour))),
        minute=max(0, min(59, int(minute))),
        day_of_week=max(0, min(6, int(day_of_week))),
        day_of_month=max(1, min(31, int(day_of_month))),
        is_enabled=True,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


def delete_scan_schedule(db: OrmSession, schedule_id: int) -> None:
    """Delete a scan schedule."""
    schedule = db.get(SecurityScanSchedule, schedule_id)
    if not schedule:
        raise ValueError(f"Scan schedule #{schedule_id} not found.")
    db.delete(schedule)
    db.commit()


def toggle_scan_schedule(db: OrmSession, schedule_id: int) -> bool:
    """Toggle a scan schedule active/inactive. Returns new is_enabled state."""
    schedule = db.get(SecurityScanSchedule, schedule_id)
    if not schedule:
        raise ValueError(f"Scan schedule #{schedule_id} not found.")
    schedule.is_enabled = not schedule.is_enabled
    db.commit()
    return schedule.is_enabled


def poll_and_run_scan_schedules(db: OrmSession) -> int:
    """Find due security scan schedules and enqueue them as background jobs."""
    from app.jobs import enqueue

    schedules = db.scalars(
        select(SecurityScanSchedule).where(SecurityScanSchedule.is_enabled == True)
    ).all()

    now = datetime.now(timezone.utc)
    enqueued_count = 0

    for s in schedules:
        if check_scan_schedule_due(s, now):
            try:
                if s.scan_type == "rkhunter":
                    if is_rkhunter_installed():
                        enqueue(
                            db,
                            "security.rkhunter_scan",
                            "Scheduled Rootkit Scan (rkhunter)",
                            payload={},
                        )
                        s.last_run_at = now
                        db.commit()
                        enqueued_count += 1
                else:
                    if is_maldet_installed():
                        enqueue(
                            db,
                            "security.maldet_scan",
                            f"Scheduled Malware Scan: {s.target_path}",
                            payload={"path": s.target_path},
                        )
                        s.last_run_at = now
                        db.commit()
                        enqueued_count += 1
            except Exception as exc:
                logger.error("Failed to enqueue scheduled scan for schedule #%d: %s", s.id, exc)

    return enqueued_count

