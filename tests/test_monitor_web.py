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

def test_monitor_page_auth_redirect(client: TestClient):
    response = client.get("/monitor", follow_redirects=False)
    assert response.status_code in (302, 303, 307)
    assert "/login" in response.headers["location"]

def test_monitor_page_authenticated(signed_in: TestClient):
    response = signed_in.get("/monitor")
    assert response.status_code == 200
    assert "Monitor" in response.text
    assert "monitor.js" in response.text

def test_monitor_live_endpoint(signed_in: TestClient):
    response = signed_in.get("/monitor/live?sort=cpu")
    assert response.status_code == 200
    data = response.json()
    assert "stats" in data
    assert "processes" in data
    assert "cpu" in data["stats"]
    assert "memory" in data["stats"]
    assert "disk" in data["stats"]
    assert isinstance(data["processes"], list)

def test_monitor_history_endpoint(signed_in: TestClient):
    response = signed_in.get("/monitor/history?range=24h")
    assert response.status_code == 200
    data = response.json()
    assert "range" in data
    assert "history" in data
    assert data["range"] == "24h"
    assert isinstance(data["history"], list)
