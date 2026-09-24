"""HTTP-level tests for the /fail2ban routes."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import fail2ban as f2b


@pytest.fixture
def client(db, admin):
    app = create_app()
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client):
    response = client.post(
        "/login", data={"username": "admin", "password": "correct-horse-battery"}
    )
    assert response.status_code == 303
    return client


def _csrf(client) -> str:
    with patch.object(f2b, "is_installed", return_value=False):
        html = client.get("/fail2ban").text
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def test_page_requires_login(client):
    response = client.get("/fail2ban")
    assert response.status_code in (302, 303, 307)
    assert "/login" in response.headers["location"]


def test_page_not_installed(signed_in):
    with patch.object(f2b, "is_installed", return_value=False):
        response = signed_in.get("/fail2ban")
    assert response.status_code == 200
    assert "Install fail2ban" in response.text
    assert "Failed attempts before a ban" in response.text
    # Sidebar regroup
    assert 'href="/fail2ban"' in response.text
    assert "Malware Scan" in response.text


def test_page_installed_shows_bans(signed_in):
    status = {
        "running": True, "jails": {"sshd": {}}, "total_failed": 12, "total_banned": 3,
        "bans": [
            {"ip": "198.51.100.4", "jail": "sshd", "banned_at": "2026-09-24 10:00:00",
             "expires_at": "2026-09-24 11:00:00", "permanent": False},
            {"ip": "192.0.2.50", "jail": f2b.MANUAL_JAIL, "banned_at": None,
             "expires_at": None, "permanent": True},
        ],
    }
    with patch.object(f2b, "is_installed", return_value=True), \
         patch.object(f2b, "get_status", return_value=status), \
         patch.object(f2b, "reapply_permanent_bans", return_value=0), \
         patch.object(f2b, "detect_ssh", return_value=f2b.SshInfo(["22"], True)):
        response = signed_in.get("/fail2ban")
    assert response.status_code == 200
    assert "198.51.100.4" in response.text
    assert "192.0.2.50" in response.text
    assert "Permanent" in response.text
    assert "key-only login" in response.text
    assert "dlg-uninstall-fail2ban" in response.text


def test_install_whitelists_admin_ip_and_enqueues(signed_in, db):
    token = _csrf(signed_in)
    response = signed_in.post("/fail2ban/install", data={"csrf_token": token},
                              headers={"X-Forwarded-For": "203.0.113.77"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")
    db.expire_all()
    assert "203.0.113.77" in f2b.get_settings(db).ignoreip


def test_save_settings_applies(signed_in, db):
    token = _csrf(signed_in)
    with patch.object(f2b, "is_installed", return_value=True), \
         patch.object(f2b, "apply_config") as apply:
        response = signed_in.post("/fail2ban/settings", data={
            "csrf_token": token, "sshd_enabled": "1", "maxretry": "3",
            "findtime": "600", "bantime": "86400", "increment_maxtime": "604800",
            "ignoreip": "10.0.0.0/8",
        })
    assert response.status_code == 303
    assert "notice=" in response.headers["location"]
    apply.assert_called_once()
    db.expire_all()
    row = f2b.get_settings(db)
    assert row.maxretry == 3
    assert row.increment_enabled is False  # unchecked box
    assert row.ignoreip == "10.0.0.0/8"


def test_save_settings_rejects_bad_input(signed_in, db):
    token = _csrf(signed_in)
    with patch.object(f2b, "apply_config") as apply:
        response = signed_in.post("/fail2ban/settings", data={
            "csrf_token": token, "maxretry": "5", "findtime": "600", "bantime": "3600",
            "increment_maxtime": "604800", "ignoreip": "nonsense",
        })
    assert "error=" in response.headers["location"]
    apply.assert_not_called()


def test_ban_refuses_own_ip(signed_in):
    token = _csrf(signed_in)
    with patch.object(f2b, "run") as run:
        response = signed_in.post("/fail2ban/ban", data={
            "csrf_token": token, "ip": "203.0.113.77", "permanent": "1",
        }, headers={"X-Forwarded-For": "203.0.113.77"})
    assert "error=" in response.headers["location"]
    run.assert_not_called()


def test_ban_and_unban(signed_in, db):
    token = _csrf(signed_in)
    with patch.object(f2b, "run") as run:
        response = signed_in.post("/fail2ban/ban", data={
            "csrf_token": token, "ip": "198.51.100.4", "permanent": "1", "note": "scanner",
        })
        assert "notice=" in response.headers["location"]
        assert [r.ip for r in f2b.list_permanent_bans(db)] == ["198.51.100.4"]

        response = signed_in.post("/fail2ban/unban", data={"csrf_token": token, "ip": "198.51.100.4"})
        assert "notice=" in response.headers["location"]
    db.expire_all()
    assert f2b.list_permanent_bans(db) == []
    assert run.call_count == 2
