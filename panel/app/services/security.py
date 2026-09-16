"""Security service: LMD (maldet) malware scanner and rkhunter rootkit scanner.

Neither tool is installed automatically. Each is opt-in via the Security page.
Scans run as background jobs so output streams live to the job detail page.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.shell import run, stream

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MALDET_BIN = "/usr/local/sbin/maldet"
MALDET_INTERNAL_BIN = "/usr/local/maldetect/maldet"
RKHUNTER_BIN = "/usr/bin/rkhunter"
MALDET_QUARANTINE_DIR = Path("/usr/local/maldetect/quarantine")
MALDET_TMP_DIR = Path("/usr/local/maldetect/tmp")
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

    for line in output.splitlines():
        # Scan ID line: "maldet(12345): {scan} scanning path: /var/www"
        m = re.search(r"maldet\((\d+)\)", line)
        if m and not scan_id:
            scan_id = m.group(1)

        # Total files scanned
        m = re.search(r"total files scanned:\s*(\d+)", line, re.IGNORECASE)
        if m:
            total_files = int(m.group(1))

        # Hit line format: "THREAT: <name> : <filepath>"
        m = re.match(r"^\{?\s*ALERT\s*\}?.*?:\s*(.+?)\s*:\s*(.+)$", line, re.IGNORECASE)
        if m:
            hits.append({"threat": m.group(1).strip(), "path": m.group(2).strip(), "severity": "high"})

    # Also try to read the maldet report file for hits if stdout parsing found none
    if not hits and scan_id:
        hits = _read_maldet_report_hits(scan_id)

    return {
        "path": path,
        "scan_id": scan_id,
        "total_files": total_files,
        "hits": len(hits),
        "hit_list": hits,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


def _read_maldet_report_hits(scan_id: str) -> list[dict]:
    """Read hits from maldet's report file for a given scan_id."""
    hits = []
    report_path = MALDET_TMP_DIR / f"report.{scan_id}"
    if not report_path.exists():
        # Try glob for partial match
        candidates = list(MALDET_TMP_DIR.glob(f"*{scan_id}*")) if MALDET_TMP_DIR.exists() else []
        if not candidates:
            return hits
        report_path = candidates[0]

    try:
        text = report_path.read_text(errors="replace")
        for line in text.splitlines():
            # Lines look like: "THREAT: hex.php.base64.1234 : /var/www/site/shell.php"
            m = re.match(r"^\s*(.+?)\s*:\s*(/.+)$", line)
            if m and "/" in m.group(2):
                hits.append({
                    "threat": m.group(1).strip(),
                    "path": m.group(2).strip(),
                    "severity": "high",
                })
    except Exception as exc:
        logger.warning("Could not read maldet report %s: %s", report_path, exc)

    return hits


def get_maldet_scan_history() -> list[dict]:
    """List recent maldet scan report IDs and metadata."""
    if not MALDET_TMP_DIR.exists():
        return []

    reports = []
    for f in sorted(MALDET_TMP_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.name.startswith("report."):
            scan_id = f.name[len("report."):]
            mtime = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
            hits = len(_read_maldet_report_hits(scan_id))
            reports.append({
                "scan_id": scan_id,
                "scanned_at": mtime.isoformat(),
                "hits": hits,
            })
        if len(reports) >= 10:
            break

    return reports


def get_maldet_report(scan_id: str) -> dict[str, Any]:
    """Return parsed hits for a specific scan_id."""
    hits = _read_maldet_report_hits(scan_id)
    return {
        "scan_id": scan_id,
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


def run_rkhunter_scan(log) -> dict[str, Any]:
    """Run rkhunter and return a parsed result dict.

    Args:
        log: callable for streaming output to the job log.

    Returns:
        dict with keys: warnings, warning_list, scanned_at
    """
    if not is_rkhunter_installed():
        raise RuntimeError("rkhunter is not installed.")

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


def _parse_rkhunter_output(output: str) -> dict[str, Any]:
    """Parse rkhunter output into structured warnings."""
    warnings: list[dict] = []

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue

        # Warning lines contain "[ Warning ]" or "Warning:"
        if "[ Warning ]" in line or line.startswith("Warning:"):
            # Clean ANSI escape codes if any remain
            clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
            if clean:
                warnings.append({"message": clean, "severity": "warning"})
        elif "[ Infected ]" in line or "Rootkit" in line:
            clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
            if clean:
                warnings.append({"message": clean, "severity": "critical"})

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
