"""HTTP-level tests for the /security routes."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


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


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


def test_security_page_redirects_when_not_logged_in(client: TestClient):
    response = client.get("/security", follow_redirects=False)
    assert response.status_code in (302, 303, 307)
    assert "/login" in response.headers["location"]


# ---------------------------------------------------------------------------
# GET /security
# ---------------------------------------------------------------------------


def test_security_page_loads_not_installed(signed_in: TestClient):
    """Security page renders correctly when neither tool is installed."""
    with patch("app.services.security.is_maldet_installed", return_value=False), \
         patch("app.services.security.is_rkhunter_installed", return_value=False):
        response = signed_in.get("/security")

    assert response.status_code == 200
    assert "Malware Scanner" in response.text
    assert "Rootkit Scanner" in response.text
    assert "Install LMD" in response.text
    assert "Install rkhunter" in response.text


def test_security_page_loads_installed(signed_in: TestClient):
    """Security page renders correctly when both tools are installed."""
    with patch("app.services.security.is_maldet_installed", return_value=True), \
         patch("app.services.security.is_rkhunter_installed", return_value=True), \
         patch("app.services.security.get_maldet_scan_history", return_value=[]), \
         patch("app.services.security.get_quarantine_list", return_value=[]), \
         patch("app.services.security.get_rkhunter_last_report", return_value=None), \
         patch("app.services.security.get_sites", return_value=["example.com"]):
        response = signed_in.get("/security")

    assert response.status_code == 200
    assert "Installed" in response.text
    assert "Run Scan" in response.text
    assert "Scan" in response.text


def test_security_page_shows_scan_history(signed_in: TestClient):
    """Scan history table rendered when history is available."""
    history = [
        {"scan_id": "abc123", "scanned_at": "2026-09-15T10:00:00", "hits": 2},
        {"scan_id": "def456", "scanned_at": "2026-09-14T08:00:00", "hits": 0},
    ]
    with patch("app.services.security.is_maldet_installed", return_value=True), \
         patch("app.services.security.is_rkhunter_installed", return_value=False), \
         patch("app.services.security.get_maldet_scan_history", return_value=history), \
         patch("app.services.security.get_quarantine_list", return_value=[]), \
         patch("app.services.security.get_sites", return_value=[]):
        response = signed_in.get("/security")

    assert response.status_code == 200
    assert "abc123" in response.text
    assert "def456" in response.text


def test_security_page_shows_quarantine(signed_in: TestClient):
    """Quarantine section rendered when quarantined files exist."""
    quarantine = [
        {"name": "shell.php.99", "quarantined_at": "2026-09-15T12:00:00", "size": 1024,
         "quarantine_path": "/usr/local/maldetect/quarantine/shell.php.99"},
    ]
    with patch("app.services.security.is_maldet_installed", return_value=True), \
         patch("app.services.security.is_rkhunter_installed", return_value=False), \
         patch("app.services.security.get_maldet_scan_history", return_value=[]), \
         patch("app.services.security.get_quarantine_list", return_value=quarantine), \
         patch("app.services.security.get_sites", return_value=[]):
        response = signed_in.get("/security")

    assert response.status_code == 200
    assert "shell.php.99" in response.text
    assert "Quarantine" in response.text


def test_security_page_shows_rkhunter_warnings(signed_in: TestClient):
    """rkhunter warnings are displayed when present."""
    report = {
        "warnings": 1,
        "warning_list": [{"message": "Warning: /bin/ls has been replaced", "severity": "warning"}],
        "scanned_at": "2026-09-15T10:00:00",
    }
    with patch("app.services.security.is_maldet_installed", return_value=False), \
         patch("app.services.security.is_rkhunter_installed", return_value=True), \
         patch("app.services.security.get_rkhunter_last_report", return_value=report), \
         patch("app.services.security.get_sites", return_value=[]):
        response = signed_in.get("/security")

    assert response.status_code == 200
    assert "/bin/ls has been replaced" in response.text


# ---------------------------------------------------------------------------
# POST routes — job enqueue
# ---------------------------------------------------------------------------


def _csrf(client: TestClient) -> str:
    """Extract CSRF token from the security page."""
    from app.database import session_scope
    from app.models import Session as DbSession
    from sqlalchemy import select
    with session_scope() as db:
        sess = db.scalars(select(DbSession)).first()
        return sess.csrf_token if sess else "test-csrf"


def test_maldet_install_enqueues_job(signed_in: TestClient, db):
    csrf = _csrf(signed_in)
    with patch("app.services.security.is_maldet_installed", return_value=False), \
         patch("app.services.security.is_rkhunter_installed", return_value=False):
        response = signed_in.post(
            "/security/maldet/install",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
    # Should redirect to a job page
    assert response.status_code == 303
    assert "/jobs/" in response.headers["location"]


def test_maldet_scan_enqueues_job(signed_in: TestClient, db):
    csrf = _csrf(signed_in)
    with patch("app.services.security.is_maldet_installed", return_value=True):
        response = signed_in.post(
            "/security/maldet/scan",
            data={"csrf_token": csrf, "path": "/var/www"},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert "/jobs/" in response.headers["location"]


def test_rkhunter_install_enqueues_job(signed_in: TestClient, db):
    csrf = _csrf(signed_in)
    with patch("app.services.security.is_rkhunter_installed", return_value=False):
        response = signed_in.post(
            "/security/rkhunter/install",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert "/jobs/" in response.headers["location"]


def test_rkhunter_scan_enqueues_job(signed_in: TestClient, db):
    csrf = _csrf(signed_in)
    with patch("app.services.security.is_rkhunter_installed", return_value=True):
        response = signed_in.post(
            "/security/rkhunter/scan",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert "/jobs/" in response.headers["location"]
