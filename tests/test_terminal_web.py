"""HTTP-level tests for the terminal's access gating.

These cover the step-up model end to end -- login alone must not be enough
to reach /terminal, the password re-entry must actually work, the grant must
expire, and cross-origin websocket connections must be refused before any
shell is ever spawned. The websocket's actual PTY behavior is covered by
test_terminal_service.py; here the point is authorization, not I/O.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import utcnow


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


def _extract_csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


# --------------------------------------------------------------------------
# Access gating
# --------------------------------------------------------------------------


def test_terminal_requires_login(client):
    response = client.get("/terminal")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_terminal_requires_step_up_even_when_logged_in(signed_in):
    """The whole point of this feature: a plain session is not enough."""
    response = signed_in.get("/terminal")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/terminal/unlock")


def test_unlock_form_renders_when_signed_in(signed_in):
    response = signed_in.get("/terminal/unlock")
    assert response.status_code == 200
    assert "Confirm your password" in response.text


def test_unlock_form_requires_login_too(client):
    response = client.get("/terminal/unlock")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --------------------------------------------------------------------------
# The unlock flow itself
# --------------------------------------------------------------------------


def test_unlock_rejects_a_post_with_no_csrf_token(signed_in):
    response = signed_in.post(
        "/terminal/unlock",
        data={"password": "correct-horse-battery", "return_to": "/terminal"},
    )
    assert response.status_code == 400
    # No grant issued from a request that never proved it came from our form.
    assert signed_in.get("/terminal").status_code == 303


def test_wrong_password_does_not_grant_unlock(signed_in):
    token = _extract_csrf(signed_in.get("/terminal/unlock").text)
    response = signed_in.post(
        "/terminal/unlock",
        data={"password": "not-the-password", "return_to": "/terminal", "csrf_token": token},
    )
    assert response.status_code == 200
    assert "Incorrect password" in response.text

    # Still gated -- a failed step-up attempt must not leak a grant.
    assert signed_in.get("/terminal").status_code == 303


def test_correct_password_grants_access(signed_in):
    page = signed_in.get("/terminal/unlock")
    token = _extract_csrf(page.text)

    response = signed_in.post(
        "/terminal/unlock",
        data={
            "password": "correct-horse-battery",
            "return_to": "/terminal",
            "csrf_token": token,
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/terminal"

    # Now the gate opens.
    assert signed_in.get("/terminal").status_code == 200


def test_unlock_without_csrf_token_is_rejected(signed_in):
    response = signed_in.post(
        "/terminal/unlock", data={"password": "correct-horse-battery", "return_to": "/terminal"}
    )
    assert response.status_code == 400
    # And critically: no grant was issued.
    assert signed_in.get("/terminal").status_code == 303


def test_repeated_wrong_passwords_lock_the_address_out(signed_in):
    token = _extract_csrf(signed_in.get("/terminal/unlock").text)
    for _ in range(5):
        signed_in.post(
            "/terminal/unlock",
            data={"password": "wrong", "return_to": "/terminal", "csrf_token": token},
        )
    response = signed_in.post(
        "/terminal/unlock",
        data={"password": "wrong", "return_to": "/terminal", "csrf_token": token},
    )
    assert response.status_code == 429


def test_return_to_cannot_be_used_as_an_open_redirect(signed_in):
    """A crafted //evil.example.com return_to must not survive re-auth."""
    page = signed_in.get("/terminal/unlock")
    token = _extract_csrf(page.text)

    response = signed_in.post(
        "/terminal/unlock",
        data={
            "password": "correct-horse-battery",
            "return_to": "//evil.example.com/steal",
            "csrf_token": token,
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/terminal"


def test_visiting_unlock_again_after_a_grant_redirects_straight_through(signed_in, db):
    from app import security
    from app.models import Session as SessionModel

    session = db.query(SessionModel).one()
    security.grant_terminal_unlock(db, session)

    response = signed_in.get("/terminal/unlock")
    assert response.status_code == 303
    assert response.headers["location"] == "/terminal"


def test_grant_expires(signed_in, db):
    from app import security
    from app.models import Session as SessionModel

    session = db.query(SessionModel).one()
    security.grant_terminal_unlock(db, session)
    assert signed_in.get("/terminal").status_code == 200

    session.terminal_unlocked_until = utcnow() - timedelta(seconds=1)
    db.commit()

    response = signed_in.get("/terminal")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/terminal/unlock")


# --------------------------------------------------------------------------
# Websocket authorization (no shell is ever spawned for a rejected attempt)
# --------------------------------------------------------------------------


def test_websocket_rejects_a_forged_origin(signed_in, db):
    from app import security
    from app.models import Session as SessionModel

    session = db.query(SessionModel).one()
    security.grant_terminal_unlock(db, session)

    with pytest.raises(Exception):
        with signed_in.websocket_connect(
            "/terminal/ws", headers={"origin": "https://evil.example.com"}
        ):
            pass


def test_websocket_rejects_without_a_session(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/terminal/ws", headers={"origin": "http://testserver"}):
            pass


def test_websocket_rejects_without_step_up_grant(signed_in):
    """Logged in, but never re-entered the password -- must still be refused."""
    with pytest.raises(Exception):
        with signed_in.websocket_connect("/terminal/ws", headers={"origin": "http://testserver"}):
            pass


def test_websocket_accepts_and_bridges_a_real_shell(signed_in, db):
    """The full happy path, including that keystrokes really reach a shell --
    proves the router wires app.services.terminal correctly, not just that
    auth passes."""
    from app import security
    from app.models import Session as SessionModel

    session = db.query(SessionModel).one()
    security.grant_terminal_unlock(db, session)

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
