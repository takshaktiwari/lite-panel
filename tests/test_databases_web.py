"""HTTP-level tests for the Databases page's "Open in Adminer" button.

The actual auto-login flow (fetching Adminer's login page, filling in the
username/db fields, resubmitting) is client-side JS (static/adminer.js) and
isn't exercised here -- these just confirm the page hands that script the
button it needs to work from, instead of the old plain link straight to
Adminer's login form.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import SiteDatabase
from app.providers import get_provider


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


@pytest.fixture(autouse=True)
def mariadb_ready(monkeypatch):
    monkeypatch.setattr(get_provider("mariadb"), "is_installed", lambda: True)


def test_databases_page_requires_login(client):
    assert client.get("/databases").status_code == 303


def test_open_in_adminer_is_a_data_attribute_button_not_a_direct_link(signed_in, db):
    db.add(SiteDatabase(db_name="demo", db_user="demo"))
    db.commit()

    response = signed_in.get("/databases")
    assert response.status_code == 200
    assert 'data-adminer-db="demo"' in response.text
    # The old direct link must be gone -- it's what forced typing credentials.
    assert 'href="/adminer/?db=demo"' not in response.text
    assert '/static/adminer.js' in response.text


def test_no_databases_yet_still_renders_cleanly(signed_in):
    response = signed_in.get("/databases")
    assert response.status_code == 200
    assert "No databases yet." in response.text


def _csrf(client) -> str:
    page = client.get("/databases")
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


def test_reset_database_password_enqueues_job(signed_in, db):
    db_record = SiteDatabase(db_name="test_db", db_user="test_user")
    db.add(db_record)
    db.commit()

    response = signed_in.post(
        f"/databases/{db_record.id}/password",
        data={"password": "new-secret-password", "csrf_token": _csrf(signed_in)},
    )
    assert response.status_code == 303
    assert "/jobs/" in response.headers["location"]


def test_reset_user_password_enqueues_job(signed_in, db):
    response = signed_in.post(
        "/databases/user/password",
        data={"db_user": "some_user", "password": "new-secret-password", "csrf_token": _csrf(signed_in)},
    )
    assert response.status_code == 303
    assert "/jobs/" in response.headers["location"]


def test_reassign_database_user_enqueues_job(signed_in, db):
    db_record = SiteDatabase(db_name="test_db", db_user="old_user")
    db.add(db_record)
    db.commit()

    response = signed_in.post(
        f"/databases/{db_record.id}/user",
        data={"user_mode": "existing", "existing_user": "new_user", "password": "", "csrf_token": _csrf(signed_in)},
    )
    assert response.status_code == 303
    assert "/jobs/" in response.headers["location"]


def test_create_database_with_existing_user_enqueues_job(signed_in, db):
    response = signed_in.post(
        "/databases/new",
        data={
            "db_name": "client_portal",
            "user_mode": "existing",
            "existing_user": "shared_user",
            "password": "",
            "site_id": "",
            "csrf_token": _csrf(signed_in),
        },
    )
    assert response.status_code == 303
    assert "/jobs/" in response.headers["location"]

