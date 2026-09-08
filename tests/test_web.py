"""End-to-end tests through the HTTP layer.

These exercise the wiring the unit tests can't: dependency resolution, the
login redirect, cookie handling and CSRF enforcement.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.security import SESSION_COOKIE


@pytest.fixture
def client(db, admin):
    """A test client sharing the fixture database.

    ``db``/``admin`` are requested so the schema exists and there is an account
    to log in as before the app starts handling requests.
    """
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
# Access control
# --------------------------------------------------------------------------


def test_dashboard_requires_login(client):
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_page_renders(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text


def test_healthz_is_public(client):
    """install.sh polls this before any account exists."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


def test_login_with_valid_credentials_sets_a_session_cookie(client):
    response = client.post(
        "/login", data={"username": "admin", "password": "correct-horse-battery"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    cookie = response.cookies.get(SESSION_COOKIE)
    assert cookie
    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie.replace("samesite", "SameSite")


def test_login_with_bad_password_is_rejected(client):
    response = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert response.status_code == 200
    assert "Invalid username or password" in response.text
    assert SESSION_COOKIE not in response.cookies


def test_login_error_does_not_reveal_whether_the_user_exists(client):
    missing = client.post("/login", data={"username": "ghost", "password": "wrong"})
    wrong = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert "Invalid username or password" in missing.text
    assert "Invalid username or password" in wrong.text


def test_repeated_failures_lock_the_address_out(client):
    for _ in range(5):
        client.post("/login", data={"username": "admin", "password": "wrong"})

    blocked = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert blocked.status_code == 429
    assert "Too many failed attempts" in blocked.text

    # Even the correct password is refused while the lockout stands.
    correct = client.post(
        "/login", data={"username": "admin", "password": "correct-horse-battery"}
    )
    assert correct.status_code == 429


# --------------------------------------------------------------------------
# Authenticated pages
# --------------------------------------------------------------------------


def test_dashboard_redirects_to_setup_before_anything_is_installed(signed_in):
    """A fresh install has no InstalledProvider rows yet, so the dashboard
    sends the operator to the setup wizard instead of an empty page."""
    response = signed_in.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def test_setup_wizard_renders(signed_in):
    """On a non-Ubuntu/Debian machine (this test runs on macOS in CI) the
    wizard correctly refuses to offer stack management rather than pretending
    it can install packages that don't apply here."""
    response = signed_in.get("/setup")
    assert response.status_code == 200
    assert "isn't Ubuntu or Debian" in response.text


def test_dashboard_renders_once_something_is_installed(signed_in, db):
    from app.models import InstalledProvider

    db.add(InstalledProvider(key="nginx", version=""))
    db.commit()

    response = signed_in.get("/")
    assert response.status_code == 200
    assert "Services" in response.text
    assert "admin" in response.text


def test_security_headers_are_applied(signed_in):
    response = signed_in.get("/")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_api_docs_are_not_exposed(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------


def test_logout_without_a_csrf_token_is_rejected(signed_in):
    response = signed_in.post("/logout", data={})
    assert response.status_code == 400
    assert "could not be verified" in response.text

    # The session must survive a rejected request.
    assert signed_in.get("/setup").status_code == 200


def test_logout_with_a_wrong_csrf_token_is_rejected(signed_in):
    response = signed_in.post("/logout", data={"csrf_token": "not-the-right-token"})
    assert response.status_code == 400


def test_logout_with_the_right_csrf_token_ends_the_session(signed_in):
    page = signed_in.get("/setup")
    token = _extract_csrf(page.text)

    response = signed_in.post("/logout", data={"csrf_token": token})
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    # The session is revoked server-side, not merely cleared in the browser.
    assert signed_in.get("/setup").status_code == 303


def test_change_password_wrong_current_password(signed_in):
    page = signed_in.get("/setup")
    token = _extract_csrf(page.text)
    response = signed_in.post(
        "/change-password",
        data={
            "csrf_token": token,
            "current_password": "wrong-password",
            "new_password": "a-new-secure-passphrase-here",
            "confirm_password": "a-new-secure-passphrase-here",
            "return_to": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=Current+password+is+incorrect" in response.headers["location"]


def test_change_password_mismatch(signed_in):
    page = signed_in.get("/setup")
    token = _extract_csrf(page.text)
    response = signed_in.post(
        "/change-password",
        data={
            "csrf_token": token,
            "current_password": "correct-horse-battery",
            "new_password": "a-new-secure-passphrase-1",
            "confirm_password": "a-new-secure-passphrase-2",
            "return_to": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=New+passwords+do+not+match" in response.headers["location"]


def test_change_password_too_short(signed_in):
    page = signed_in.get("/setup")
    token = _extract_csrf(page.text)
    response = signed_in.post(
        "/change-password",
        data={
            "csrf_token": token,
            "current_password": "correct-horse-battery",
            "new_password": "short",
            "confirm_password": "short",
            "return_to": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_change_password_success(signed_in):
    page = signed_in.get("/setup")
    token = _extract_csrf(page.text)
    new_pass = "brand-new-secure-passphrase-2026"
    response = signed_in.post(
        "/change-password",
        data={
            "csrf_token": token,
            "current_password": "correct-horse-battery",
            "new_password": new_pass,
            "confirm_password": new_pass,
            "return_to": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "notice=Password+updated+successfully" in response.headers["location"]

    # Now verify login works with the new password
    login_resp = signed_in.post(
        "/login",
        data={"username": "admin", "password": new_pass},
        follow_redirects=False,
    )
    assert login_resp.status_code == 303
    assert login_resp.headers["location"] == "/"


def _extract_csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]

