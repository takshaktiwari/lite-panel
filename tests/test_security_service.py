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
[22:00:01] Info: Starting test name 'system_commands'
[22:00:01] Checking 'ls'... [ OK ]
[22:00:01] Checking 'ps'... [ OK ]
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
        "PATH: /var/www\n"
        "TOTAL FILES: 45\n"
        "TOTAL HITS: 1\n"
    )
    (tmp_path / "session.last").write_text("260916-0810.1234")
    (tmp_path / "session.hits.260916-0810.1234").write_text("{HEX}shell.php : /var/www/site/shell.php\n")

    with patch("app.services.security.MALDET_SESS_DIR", tmp_path):
        history = sec.get_maldet_scan_history()

    assert len(history) == 1
    assert history[0]["scan_id"] == "260916-0810.1234"
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
