"""Tests for the config editor routes and service."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import config_editor as svc


# ---------------------------------------------------------------------------
# Service-level unit tests (no HTTP, no real filesystem paths)
# ---------------------------------------------------------------------------


def test_backup_creates_file(tmp_path):
    target = tmp_path / "test.conf"
    target.write_text("original content", encoding="utf-8")
    backup = svc.backup_config(target)
    assert backup is not None
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == "original content"


def test_backup_rolls_over_max(tmp_path):
    target = tmp_path / "test.conf"
    target.write_text("v0", encoding="utf-8")
    # Create MAX_BACKUPS + 2 backups; expect only MAX_BACKUPS to be kept via
    # the rotating index — oldest slot gets overwritten.
    for i in range(svc.MAX_BACKUPS + 2):
        target.write_text(f"v{i}", encoding="utf-8")
        svc.backup_config(target)
    existing = list(tmp_path.glob("test.conf.backup.*"))
    assert len(existing) <= svc.MAX_BACKUPS


def test_restore_latest_backup(tmp_path):
    target = tmp_path / "test.conf"
    target.write_text("original", encoding="utf-8")
    svc.backup_config(target)
    target.write_text("broken", encoding="utf-8")
    ok = svc.restore_latest_backup(target)
    assert ok
    assert target.read_text(encoding="utf-8") == "original"


def test_restore_without_backup_returns_false(tmp_path):
    target = tmp_path / "nobackup.conf"
    target.write_text("data", encoding="utf-8")
    ok = svc.restore_latest_backup(target)
    assert not ok


def test_save_with_rollback_restores_on_validation_failure(tmp_path):
    target = tmp_path / "nginx.conf"
    target.write_text("good content", encoding="utf-8")

    def bad_validator():
        return False, "syntax error on line 1"

    def noop_reloader():
        return True, "reloaded"

    with pytest.raises(svc.SaveError, match="Validation failed"):
        svc._save_with_rollback(
            path=target,
            content="bad content",
            validator=bad_validator,
            reloader=noop_reloader,
            label="test.conf",
        )

    # Content must be restored.
    assert target.read_text(encoding="utf-8") == "good content"


def test_save_with_rollback_restores_on_reload_failure(tmp_path):
    target = tmp_path / "nginx.conf"
    target.write_text("original", encoding="utf-8")

    def ok_validator():
        return True, "OK"

    def bad_reloader():
        return False, "service failed to start"

    with pytest.raises(svc.SaveError, match="Service reload failed"):
        svc._save_with_rollback(
            path=target,
            content="new content",
            validator=ok_validator,
            reloader=bad_reloader,
            label="test.conf",
        )

    assert target.read_text(encoding="utf-8") == "original"


def test_save_with_rollback_succeeds(tmp_path):
    target = tmp_path / "nginx.conf"
    target.write_text("original", encoding="utf-8")

    def ok_validator():
        return True, "OK"

    def ok_reloader():
        return True, "reloaded"

    msg = svc._save_with_rollback(
        path=target,
        content="new content",
        validator=ok_validator,
        reloader=ok_reloader,
        label="test.conf",
    )
    assert msg == "reloaded"
    assert target.read_text(encoding="utf-8") == "new content"


# ---------------------------------------------------------------------------
# HTTP integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db, admin):
    app = create_app()
    from fastapi.testclient import TestClient
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def signed_in(client):
    client.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
    return client


def test_config_index_requires_login(client):
    r = client.get("/config")
    assert r.status_code == 303


def test_config_index_renders(signed_in):
    r = signed_in.get("/config")
    assert r.status_code == 200
    assert "Config Editor" in r.text


def test_nginx_editor_requires_login(client):
    r = client.get("/config/nginx")
    assert r.status_code == 303


def test_nginx_editor_renders(signed_in):
    # nginx.conf may not exist in dev; service returns empty string — still renders.
    r = signed_in.get("/config/nginx")
    assert r.status_code == 200
    assert "nginx.conf" in r.text
    assert "CodeMirror" in r.text or "codemirror" in r.text.lower()


def test_php_editor_unknown_version(signed_in):
    r = signed_in.get("/config/php/99.9")
    # Should render an error (400) or redirect — not a 500.
    assert r.status_code in (200, 303, 400, 404)


def test_nginx_save_csrf_required(signed_in):
    # Submit without a valid CSRF token → should be rejected (400 or redirect).
    r = signed_in.post("/config/nginx", data={"content": "server {}", "csrf_token": "bad"})
    assert r.status_code in (303, 400)
