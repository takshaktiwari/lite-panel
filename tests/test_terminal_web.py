"""HTTP-level tests for the terminal's access gating.

A valid panel session is enough to open the terminal, the same bar as every
other page. The one extra control is on the websocket itself: it only
accepts same-origin connections, since a cross-origin WebSocket handshake
still carries cookies (unlike a form POST, which CSRF tokens cover). The
websocket's actual PTY behavior is covered by test_terminal_service.py; here
the point is authorization, not I/O.
"""

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


# --------------------------------------------------------------------------
# Access gating
# --------------------------------------------------------------------------


def test_terminal_requires_login(client):
    response = client.get("/terminal")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_terminal_is_reachable_with_a_plain_session(signed_in):
    """No step-up re-authentication -- a normal session is sufficient, the
    same bar as every other authenticated page in the panel."""
    response = signed_in.get("/terminal")
    assert response.status_code == 200
    assert "xterm.js" in response.text


def test_unlock_route_no_longer_exists(signed_in):
    """The step-up flow was removed outright, not just bypassed."""
    assert signed_in.get("/terminal/unlock").status_code == 404


# --------------------------------------------------------------------------
# Websocket authorization (no shell is ever spawned for a rejected attempt)
# --------------------------------------------------------------------------


def test_websocket_rejects_a_forged_origin(signed_in):
    with pytest.raises(Exception):
        with signed_in.websocket_connect(
            "/terminal/ws", headers={"origin": "https://evil.example.com"}
        ):
            pass


def test_websocket_accepts_origin_with_port(signed_in):
    # Simulates browser sending Origin with custom port while proxy sends Host without port
    with signed_in.websocket_connect(
        "/terminal/ws",
        headers={"origin": "https://testserver:8443", "host": "testserver"},
    ):
        pass


def test_websocket_rejects_without_a_session(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/terminal/ws", headers={"origin": "http://testserver"}):
            pass


def test_websocket_accepts_with_a_plain_session(signed_in):
    """A logged-in session alone is enough to open the websocket -- no
    separate password re-entry required."""
    with signed_in.websocket_connect("/terminal/ws", headers={"origin": "http://testserver"}):
        pass  # connecting at all, without raising, is the assertion


def test_websocket_accepts_and_bridges_a_real_shell(signed_in):
    """The full happy path, including that keystrokes really reach a shell --
    proves the router wires app.services.terminal correctly, not just that
    auth passes."""
    with signed_in.websocket_connect("/terminal/ws", headers={"origin": "http://testserver"}) as ws:
        ws.send_bytes(b"echo hello-from-ws-test\n")

        collected = b""
        for _ in range(50):
            try:
                chunk = ws.receive_bytes()
                collected += chunk
                if b"hello-from-ws-test" in collected:
                    break
            except Exception:
                break

        assert b"hello-from-ws-test" in collected
