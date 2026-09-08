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
