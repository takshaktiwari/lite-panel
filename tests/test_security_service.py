"""Tests for panel/app/services/security.py."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services import security as sec


# ---------------------------------------------------------------------------
# is_maldet_installed / is_rkhunter_installed
# ---------------------------------------------------------------------------


def test_maldet_not_installed_when_no_binary(tmp_path):
    with patch("app.services.security.os.path.isfile", return_value=False), \
         patch("app.services.security.shutil.which", return_value=None):
        assert sec.is_maldet_installed() is False


def test_maldet_installed_when_binary_exists():
    with patch("app.services.security.os.path.isfile", return_value=True):
        assert sec.is_maldet_installed() is True


def test_maldet_installed_when_on_path():
    with patch("app.services.security.os.path.isfile", return_value=False), \
         patch("app.services.security.shutil.which", return_value="/usr/local/sbin/maldet"):
        assert sec.is_maldet_installed() is True


def test_rkhunter_not_installed_when_no_binary():
    with patch("app.services.security.os.path.isfile", return_value=False), \
         patch("app.services.security.shutil.which", return_value=None):
        assert sec.is_rkhunter_installed() is False


def test_rkhunter_installed_when_binary_exists():
    with patch("app.services.security.os.path.isfile", return_value=True):
        assert sec.is_rkhunter_installed() is True


# ---------------------------------------------------------------------------
# _parse_maldet_output
# ---------------------------------------------------------------------------


MALDET_OUTPUT_CLEAN = """
maldet(12345): {scan} scanning path: /var/www
maldet(12345): {scan} total files scanned: 1234
maldet(12345): {scan} total hits found: 0
"""

MALDET_OUTPUT_HITS = """
maldet(99999): {scan} scanning path: /var/www
maldet(99999): {scan} total files scanned: 500
ALERT: hex.php.webshell.123 : /var/www/example.com/shell.php
ALERT: php.cmdshell.generic.1 : /var/www/other.com/backdoor.php
"""


def test_parse_maldet_output_clean():
    result = sec._parse_maldet_output(MALDET_OUTPUT_CLEAN, "/var/www")
    assert result["path"] == "/var/www"
    assert result["total_files"] == 1234
    assert result["hits"] == 0
    assert result["hit_list"] == []


def test_parse_maldet_output_with_hits():
    result = sec._parse_maldet_output(MALDET_OUTPUT_HITS, "/var/www")
    assert result["scan_id"] == "99999"
    assert result["hits"] == 2
    assert any("shell.php" in h["path"] for h in result["hit_list"])
    assert any("backdoor.php" in h["path"] for h in result["hit_list"])


def test_parse_maldet_output_no_scan_id():
    result = sec._parse_maldet_output("some random output", "/tmp")
    assert result["path"] == "/tmp"
    assert result["hits"] == 0


# ---------------------------------------------------------------------------
# _parse_rkhunter_output
# ---------------------------------------------------------------------------


RKHUNTER_CLEAN = """
[09:00:11] Running Rootkit Hunter version 1.4.6 on bee1
[22:00:01] Info: Starting test name 'system_commands'
[22:00:01] Checking 'ls'... [ OK ]
[22:00:01] Checking 'ps'... [ OK ]
Rootkits checked : 498
Possible rootkits: 0
"""

RKHUNTER_WARNINGS = """
[22:00:01] Warning: The command '/bin/ls' has been replaced by a script
[22:00:02] Warning: Suspicious file permissions found for '/etc/passwd'
[22:00:03] [ Warning ] Possible rootkit: '/dev/.hidden'
"""


def test_parse_rkhunter_output_clean():
    result = sec._parse_rkhunter_output(RKHUNTER_CLEAN)
    assert result["warnings"] == 0
    assert result["warning_list"] == []


def test_parse_rkhunter_output_with_warnings():
    result = sec._parse_rkhunter_output(RKHUNTER_WARNINGS)
    assert result["warnings"] >= 1
    messages = " ".join(w["message"] for w in result["warning_list"])
    assert any(keyword in messages for keyword in ("ls", "passwd", "rootkit", "Warning", "hidden"))


def test_parse_rkhunter_filters_benign_ubuntu_noise():
    noisy_output = """
[09:06:27] Checking for passwd file changes [ Warning ]
[09:06:27] Warning: User 'postfix' has been added to the passwd file.
[09:06:27] Checking for group file changes [ Warning ]
[09:06:27] Warning: Group 'postfix' has been added to the group file.
[09:06:27] Warning: Group 'postdrop' has been added to the group file.
[09:06:27] Checking if SSH root access is allowed [ Warning ]
[09:06:27] Warning: The SSH and rkhunter configuration options should be the same:
[09:06:31] Checking /dev for suspicious file types [ Warning ]
[09:06:31] Warning: Suspicious file types found in /dev:
[09:06:31] Checking for hidden files and directories [ Warning ]
[09:06:31] Warning: Hidden file found: /etc/.resolv.conf.systemd-resolved.bak: ASCII text
[09:06:31] Warning: Hidden file found: /etc/.updated: ASCII text
"""
    result = sec._parse_rkhunter_output(noisy_output)
    assert result["warnings"] == 0
    assert result["warning_list"] == []


def test_parse_rkhunter_output_returns_scanned_at():
    result = sec._parse_rkhunter_output("")
    assert "scanned_at" in result


# ---------------------------------------------------------------------------
# get_sites
# ---------------------------------------------------------------------------


def test_get_sites_returns_empty_when_no_www(tmp_path):
    with patch("app.services.security.SITES_DIR", str(tmp_path / "nonexistent")):
        assert sec.get_sites() == []


def test_get_sites_lists_directories(tmp_path):
    (tmp_path / "example.com").mkdir()
    (tmp_path / "another.com").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x")

    with patch("app.services.security.SITES_DIR", str(tmp_path)):
        sites = sec.get_sites()

    assert "example.com" in sites
    assert "another.com" in sites
    assert ".hidden" not in sites  # hidden dirs excluded
    assert "file.txt" not in sites  # files excluded


# ---------------------------------------------------------------------------
# get_quarantine_list
# ---------------------------------------------------------------------------


def test_get_quarantine_list_returns_empty_when_dir_missing():
    with patch("app.services.security.MALDET_QUARANTINE_DIR", Path("/nonexistent/path")):
        assert sec.get_quarantine_list() == []


def test_get_quarantine_list_returns_files(tmp_path):
    (tmp_path / "shell.php.123").write_text("<?php system($_GET['cmd']); ?>")
    with patch("app.services.security.MALDET_QUARANTINE_DIR", tmp_path):
        items = sec.get_quarantine_list()
    assert len(items) == 1
    assert items[0]["name"] == "shell.php.123"
    assert "quarantined_at" in items[0]
    assert items[0]["size"] > 0


# ---------------------------------------------------------------------------
# delete_file
# ---------------------------------------------------------------------------


def test_delete_file_removes_file(tmp_path):
    f = tmp_path / "malware.php"
    f.write_text("evil")
    sec.delete_file(str(f))
    assert not f.exists()


def test_delete_file_raises_when_not_found():
    with pytest.raises(ValueError, match="File not found"):
        sec.delete_file("/nonexistent/file.php")


# ---------------------------------------------------------------------------
# delete_quarantine_file
# ---------------------------------------------------------------------------


def test_delete_quarantine_file(tmp_path):
    f = tmp_path / "shell.php.99"
    f.write_text("evil")
    with patch("app.services.security.MALDET_QUARANTINE_DIR", tmp_path):
        sec.delete_quarantine_file("shell.php.99")
    assert not f.exists()


def test_delete_quarantine_file_raises_when_not_found(tmp_path):
    with patch("app.services.security.MALDET_QUARANTINE_DIR", tmp_path):
        with pytest.raises(ValueError, match="not found"):
            sec.delete_quarantine_file("nonexistent.php")


# ---------------------------------------------------------------------------
# get_maldet_scan_history
# ---------------------------------------------------------------------------


def test_get_maldet_scan_history_empty_when_dir_missing():
    with patch("app.services.security.MALDET_SESS_DIR", Path("/nonexistent")), \
         patch("app.services.security.MALDET_TMP_DIR", Path("/nonexistent")):
        assert sec.get_maldet_scan_history() == []


def test_get_maldet_scan_history_lists_reports(tmp_path):
    (tmp_path / "report.abc123").write_text("some report")
    (tmp_path / "report.def456").write_text("another report")
    (tmp_path / "other_file").write_text("ignored")

    with patch("app.services.security.MALDET_SESS_DIR", Path("/nonexistent")), \
         patch("app.services.security.MALDET_TMP_DIR", tmp_path):
        history = sec.get_maldet_scan_history()

    scan_ids = {r["scan_id"] for r in history}
    assert "abc123" in scan_ids
    assert "def456" in scan_ids
    assert len(history) == 2


def test_get_maldet_scan_history_parses_session_files(tmp_path):
    sess_file = tmp_path / "session.260916-0810.1234"
    sess_file.write_text(
        "SCAN ID: 260916-0810.1234\n"
        "PATH: /var/www/dreame_bee1_online\n"
        "TOTAL FILES: 45\n"
        "TOTAL HITS: 1\n"
    )
    (tmp_path / "session.last").write_text("260916-0810.1234")
    (tmp_path / "session.hits.260916-0810.1234").write_text("{HEX}shell.php : /var/www/dreame_bee1_online/shell.php\n")

    with patch("app.services.security.MALDET_SESS_DIR", tmp_path):
        history = sec.get_maldet_scan_history()

    assert len(history) == 1
    assert history[0]["scan_id"] == "260916-0810.1234"
    assert history[0]["path"] == "/var/www/dreame_bee1_online"
    assert history[0]["total_files"] == 45
    assert history[0]["hits"] == 1


# ---------------------------------------------------------------------------
# get_rkhunter_last_report
# ---------------------------------------------------------------------------


def test_get_rkhunter_last_report_returns_none_when_no_log():
    with patch("app.services.security.RKHUNTER_LOG", Path("/nonexistent/rkhunter.log")):
        assert sec.get_rkhunter_last_report() is None


def test_get_rkhunter_last_report_parses_log(tmp_path):
    log = tmp_path / "rkhunter.log"
    log.write_text("Warning: The command '/bin/ls' has been replaced by a script\n")
    with patch("app.services.security.RKHUNTER_LOG", log):
        report = sec.get_rkhunter_last_report()
    assert report is not None
    assert report["warnings"] >= 1
    assert "scanned_at" in report


# ---------------------------------------------------------------------------
# install_maldet / install_rkhunter
# ---------------------------------------------------------------------------


def test_install_maldet_sets_cwd_to_install_dir(tmp_path):
    calls = []

    def mock_run(args, **kwargs):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.returncode = 0
        if "curl" in args:
            # Simulate downloaded tarball
            tarball = Path(args[args.index("-o") + 1])
            tarball.touch()
        elif "tar" in args:
            # Simulate extracted directory
            target_dir = Path(args[args.index("-C") + 1])
            (target_dir / "maldetect-1.6.6").mkdir(parents=True, exist_ok=True)
            (target_dir / "maldetect-1.6.6" / "install.sh").touch()
        return m

    stream_calls = []

    def mock_stream(args, log_fn, **kwargs):
        stream_calls.append({"args": args, "kwargs": kwargs})
        return 0

    logs = []
    with patch("app.services.security.run", side_effect=mock_run), \
         patch("app.services.security.stream", side_effect=mock_stream), \
         patch("app.services.security.is_maldet_installed", return_value=True):
        sec.install_maldet(logs.append)

    assert len(stream_calls) == 1
    call = stream_calls[0]
    assert call["args"] == ["bash", "install.sh"]
    assert "maldetect-1.6.6" in call["kwargs"]["cwd"]


def test_install_rkhunter_success():
    stream_calls = []

    def mock_stream(args, log_fn, **kwargs):
        stream_calls.append(args)
        return 0

    logs = []
    with patch("app.services.security.run") as mock_run, \
         patch("app.services.security.stream", side_effect=mock_stream), \
         patch("app.services.security.is_rkhunter_installed", return_value=True):
        sec.install_rkhunter(logs.append)

    assert any("rkhunter" in " ".join(c) for c in stream_calls)
    assert any("LMD" in l or "rkhunter installed" in l for l in logs)


def test_uninstall_maldet_success():
    logs = []
    with patch("app.services.security.run") as mock_run, \
         patch("app.services.security.stream") as mock_stream, \
         patch("pathlib.Path.exists", return_value=False):
        sec.uninstall_maldet(logs.append)
    assert any("removed" in l.lower() or "uninstall" in l.lower() for l in logs)


def test_uninstall_rkhunter_success():
    logs = []
    with patch("app.services.security.stream", return_value=0) as mock_stream, \
         patch("pathlib.Path.exists", return_value=False):
        sec.uninstall_rkhunter(logs.append)
    assert any("uninstall" in l.lower() for l in logs)


# ---------------------------------------------------------------------------
# Scan Schedules Service Tests
# ---------------------------------------------------------------------------


from datetime import datetime, timezone
from app.models import SecurityScanSchedule


def test_describe_scan_schedule():
    s_daily = SecurityScanSchedule(scan_type="maldet", frequency="daily", hour=3, minute=30)
    assert sec.describe_scan_schedule(s_daily) == "Daily at 03:30 UTC"

    s_weekly = SecurityScanSchedule(scan_type="maldet", frequency="weekly", hour=4, minute=0, day_of_week=6)
    assert sec.describe_scan_schedule(s_weekly) == "Weekly on Sunday at 04:00 UTC"

    s_monthly = SecurityScanSchedule(scan_type="rkhunter", frequency="monthly", hour=1, minute=15, day_of_month=1)
    assert sec.describe_scan_schedule(s_monthly) == "Monthly on day 1st at 01:15 UTC"


def test_get_next_scan_run():
    ref_time = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)  # Wednesday

    # Daily: later today
    s1 = SecurityScanSchedule(frequency="daily", hour=12, minute=0)
    next_s1 = sec.get_next_scan_run(s1, ref_time)
    assert next_s1 == datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)

    # Daily: already passed today -> tomorrow
    s2 = SecurityScanSchedule(frequency="daily", hour=8, minute=0)
    next_s2 = sec.get_next_scan_run(s2, ref_time)
    assert next_s2 == datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc)

    # Weekly: upcoming Sunday (day_of_week=6)
    s3 = SecurityScanSchedule(frequency="weekly", hour=2, minute=0, day_of_week=6)
    next_s3 = sec.get_next_scan_run(s3, ref_time)
    assert next_s3.weekday() == 6
    assert next_s3 > ref_time


def test_check_scan_schedule_due():
    now = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)

    # Disabled schedule is never due
    s_off = SecurityScanSchedule(is_enabled=False, frequency="daily", hour=2, minute=0)
    assert sec.check_scan_schedule_due(s_off, now) is False

    # Daily schedule at 2 AM, hasn't run yet -> due
    s_daily = SecurityScanSchedule(is_enabled=True, frequency="daily", hour=2, minute=0, last_run_at=None)
    assert sec.check_scan_schedule_due(s_daily, now) is True

    # Daily schedule already ran at 2:05 AM today -> not due
    s_daily.last_run_at = datetime(2026, 9, 16, 2, 5, tzinfo=timezone.utc)
    assert sec.check_scan_schedule_due(s_daily, now) is False


def test_create_and_manage_scan_schedules(db):
    sched = sec.create_scan_schedule(
        db,
        scan_type="maldet",
        target_path="/var/www/mysite.com",
        frequency="weekly",
        hour=3,
        minute=0,
        day_of_week=6,
    )
    assert sched.id is not None
    assert sched.target_path == "/var/www/mysite.com"
    assert sched.is_enabled is True

    schedules = sec.get_scan_schedules(db)
    assert len(schedules) == 1
    assert schedules[0]["id"] == sched.id
    assert schedules[0]["scan_type"] == "maldet"
    assert schedules[0]["target_path"] == "/var/www/mysite.com"

    # Toggle
    state = sec.toggle_scan_schedule(db, sched.id)
    assert state is False
    assert sec.get_scan_schedules(db)[0]["is_enabled"] is False

    # Delete
    sec.delete_scan_schedule(db, sched.id)
    assert len(sec.get_scan_schedules(db)) == 0


def test_poll_and_run_scan_schedules(db):
    # Create due schedule
    sched = sec.create_scan_schedule(
        db,
        scan_type="maldet",
        target_path="/var/www",
        frequency="daily",
        hour=0,
        minute=0,
    )

    with patch("app.services.security.is_maldet_installed", return_value=True), \
         patch("app.jobs.enqueue") as mock_enqueue:
        enqueued = sec.poll_and_run_scan_schedules(db)
        assert enqueued == 1
        assert mock_enqueue.called
        assert sched.last_run_at is not None

