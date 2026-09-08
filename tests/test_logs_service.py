"""Tests for log viewing service: safe path verification, file tailing, and journals."""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import logs as logs_service
from app.validators import ValidationError


def test_service_log_targets_defined():
    targets = logs_service.get_service_log_targets()
    assert len(targets) >= 5
    ids = {t.id for t in targets}
    assert "nginx_error" in ids
    assert "mariadb_journal" in ids
    assert "panel_log" in ids


def test_site_log_targets_for_valid_site():
    targets = logs_service.get_site_log_targets("demo")
    assert len(targets) == 3
    ids = {t.id for t in targets}
    assert "site_demo_error" in ids
    assert "site_demo_access" in ids
    assert "site_demo_php_error" in ids


def test_site_log_targets_rejects_invalid_site_name():
    with pytest.raises(ValidationError):
        logs_service.get_site_log_targets("../evil")


def test_tail_file_refuses_disallowed_paths():
    with pytest.raises(ValidationError):
        logs_service.tail_file(Path("/etc/shadow"))
    with pytest.raises(ValidationError):
        logs_service.tail_file(Path("/etc/passwd"))


def test_tail_journal_refuses_disallowed_services():
    with pytest.raises(ValidationError):
        logs_service.tail_journal("ssh")
    with pytest.raises(ValidationError):
        logs_service.tail_journal("systemd")


def test_tail_file_missing_file_returns_clean_message(tmp_path):
    missing = Path("/var/log/does-not-exist.log")
    out = logs_service.tail_file(missing)
    assert "does not exist" in out
