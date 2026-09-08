"""Tests for logs web endpoints."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Site


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


def test_logs_page_requires_auth(client):
    assert client.get("/logs").status_code == 303


def test_logs_page_renders_service_logs(signed_in):
    res = signed_in.get("/logs")
    assert res.status_code == 200
    assert "Live logs for system services and hosted websites" in res.text
    assert "Nginx Error Log" in res.text
    assert "/static/logs.js" in res.text


def test_logs_page_renders_with_site_selected(signed_in, db):
    site = Site(
        name="myblog",
        domain="myblog.test",
        system_user="site_myblog",
        root_dir="/var/www/myblog",
        webroot="/var/www/myblog",
    )
    db.add(site)
    db.commit()

    res = signed_in.get("/logs?source=site&name=myblog")
    assert res.status_code == 200
    assert "myblog.test" in res.text
    assert "PHP Error Log" in res.text


def test_logs_content_api_returns_json(signed_in):
    res = signed_in.get("/logs/content?source=service&log_id=panel_log&lines=50")
    assert res.status_code == 200
    data = res.json()
    assert "content" in data
