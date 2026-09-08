"""HTTP-level tests for the FTP router.

The service layer (test_ftp_service.py) covers the real behavior in depth;
these confirm the routes are wired to it correctly -- auth, CSRF, and that a
submission enqueues the right job with the right payload. The worker is
never started (see test_cron_web.py), so no real useradd/chown ever runs
here.
"""

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import jobs
from app.main import create_app
from app.models import FtpAccount, Job, Site


@pytest.fixture(autouse=True)
def isolated_worker_queue(monkeypatch):
    monkeypatch.setattr(jobs.worker, "submit", lambda job_id: None)


@pytest.fixture(autouse=True)
def vsftpd_installed(monkeypatch):
    """The page (and /ftp/create) gate everything behind vsftpd being
    installed; these tests are about routing/validation, not apt, so vsftpd
    is faked as present -- there's no dpkg-query on the machine running
    these tests anyway."""
    fake = MagicMock()
    fake.is_installed.return_value = True
    monkeypatch.setattr("app.routers.ftp.get_provider", lambda key: fake)


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


def _last_job(db) -> Job:
    return db.scalars(select(Job).order_by(Job.id.desc())).first()


def _csrf(client) -> str:
    page = client.get("/ftp")
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


# --------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------


def test_ftp_page_requires_login(client):
    assert client.get("/ftp").status_code == 303


def test_create_requires_csrf(signed_in):
    response = signed_in.post(
        "/ftp/create", data={"username": "bob", "password": "x", "path": "/var/www/bob"}
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------
# Page rendering
# --------------------------------------------------------------------------


def test_page_lists_every_site_in_the_picker(signed_in, db):
    site = Site(
        name="moly", domain="moly.example", system_user="site_moly",
        root_dir="/var/www/moly", webroot="/var/www/moly",
    )
    db.add(site)
    db.commit()

    page = signed_in.get("/ftp")
    assert page.status_code == 200
    assert "moly.example" in page.text
    assert "site_moly" in page.text


def test_page_shows_host_username_and_path_for_each_account(signed_in, db):
    account = FtpAccount(site_id=None, username="bob", home_dir="/var/www/shared-uploads")
    db.add(account)
    db.commit()

    page = signed_in.get("/ftp")
    assert "bob" in page.text
    assert "/var/www/shared-uploads" in page.text


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------


def test_create_enqueues_a_job_with_the_submitted_fields(signed_in, db):
    token = _csrf(signed_in)
    response = signed_in.post(
        "/ftp/create",
        data={
            "username": "bob",
            "password": "correct-horse",
            "path": "/var/www/shared-uploads",
            "csrf_token": token,
        },
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")

    job = _last_job(db)
    assert job.kind == "ftp.create"
    payload = json.loads(job.payload)
    assert payload["username"] == "bob"
    assert payload["path"] == "/var/www/shared-uploads"


def test_create_without_a_path_is_rejected_before_enqueueing(signed_in, db):
    token = _csrf(signed_in)
    response = signed_in.post(
        "/ftp/create",
        data={"username": "bob", "password": "x", "path": "  ", "csrf_token": token},
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert _last_job(db) is None


def test_create_without_a_password_is_rejected_before_enqueueing(signed_in, db):
    token = _csrf(signed_in)
    response = signed_in.post(
        "/ftp/create",
        data={"username": "bob", "password": "", "path": "/var/www/x", "csrf_token": token},
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert _last_job(db) is None


# --------------------------------------------------------------------------
# Delete -- must never be able to touch the filesystem from this layer
# --------------------------------------------------------------------------


def test_delete_enqueues_a_job_for_the_right_account(signed_in, db):
    account = FtpAccount(site_id=None, username="bob", home_dir="/var/www/shared-uploads")
    db.add(account)
    db.commit()
    token = _csrf(signed_in)

    response = signed_in.post(f"/ftp/{account.id}/delete", data={"csrf_token": token})

    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")
    job = _last_job(db)
    assert job.kind == "ftp.delete"
    assert json.loads(job.payload)["account_id"] == account.id


def test_delete_unknown_account_is_a_clean_error(signed_in, db):
    token = _csrf(signed_in)
    response = signed_in.post("/ftp/999999/delete", data={"csrf_token": token})
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


# --------------------------------------------------------------------------
# Password change
# --------------------------------------------------------------------------


def test_change_password_enqueues_a_job(signed_in, db):
    account = FtpAccount(site_id=None, username="bob", home_dir="/var/www/shared-uploads")
    db.add(account)
    db.commit()
    token = _csrf(signed_in)

    response = signed_in.post(
        f"/ftp/{account.id}/password",
        data={"password": "new-password", "csrf_token": token},
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")
