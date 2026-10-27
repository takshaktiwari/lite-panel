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


# --------------------------------------------------------------------------
# Choosing which account the terminal logs in as
# --------------------------------------------------------------------------

import getpass  # noqa: E402

from app.models import Site  # noqa: E402


@pytest.fixture
def two_sites(db):
    for name in ("shop", "blog"):
        db.add(Site(name=name, domain=f"{name}.example.com", system_user=f"site_{name}",
                    root_dir=f"/var/www/{name}", webroot=f"/var/www/{name}"))
    db.commit()


def _options(html):
    start = html.index('id="terminal-login"')
    block = html[start : html.index("</select>", start)]
    return [chunk.split('"', 1)[0] for chunk in block.split('value="')[1:]]


def test_dropdown_lists_every_site_alphabetically_with_root_last(signed_in, two_sites):
    page = signed_in.get("/terminal")
    assert _options(page.text) == ["blog", "shop", "root"]
    assert "blog.example.com (site_blog)" in page.text
    assert "root (full server access)" in page.text


def test_root_is_selected_by_default(signed_in, two_sites):
    page = signed_in.get("/terminal")
    assert 'data-login-as="root"' in page.text
    assert 'value="root" selected' in page.text


def test_explicit_choice_selects_that_site_user(signed_in, two_sites):
    page = signed_in.get("/terminal?as=shop")
    assert 'data-login-as="shop"' in page.text
    assert "site_shop" in page.text


def test_a_site_path_in_cd_selects_that_sites_user(signed_in, two_sites):
    page = signed_in.get("/terminal?cd=/var/www/blog/public")
    assert 'data-login-as="blog"' in page.text


def test_a_lookalike_path_does_not_match_a_site(signed_in, two_sites):
    page = signed_in.get("/terminal?cd=/var/www/blogger")
    assert 'data-login-as="root"' in page.text


def test_unknown_choice_falls_back_to_root(signed_in, two_sites):
    assert 'data-login-as="root"' in signed_in.get("/terminal?as=nobody").text


def test_websocket_rejects_an_account_that_is_not_a_site(signed_in, two_sites):
    """A raw system username is never accepted -- only root or a site name."""
    with pytest.raises(Exception):
        with signed_in.websocket_connect(
            "/terminal/ws?as=daemon", headers={"origin": "http://testserver"}
        ):
            pass


def test_websocket_opens_the_shell_as_the_chosen_sites_user(signed_in, db, monkeypatch):
    db.add(Site(name="me", domain="me.example.com", system_user=getpass.getuser(),
                root_dir="/nonexistent-dir", webroot="/nonexistent-dir"))
    db.commit()
    seen = {}

    from app.routers import terminal as router
    real = router.TerminalSession

    def spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(router, "TerminalSession", spy)
    with signed_in.websocket_connect(
        "/terminal/ws?as=me", headers={"origin": "http://testserver"}
    ) as ws:
        ws.send_bytes(b"echo $USER-$HOME\n")
        collected = b""
        for _ in range(50):
            collected += ws.receive_bytes()
            if f"{getpass.getuser()}-".encode() in collected.split(b"\n", 1)[-1]:
                break

    assert seen["user"] == getpass.getuser()
    assert getpass.getuser().encode() in collected


def test_websocket_default_is_root_style_shell_without_a_user(signed_in, monkeypatch):
    seen = {}
    from app.routers import terminal as router
    real = router.TerminalSession

    def spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(router, "TerminalSession", spy)
    with signed_in.websocket_connect("/terminal/ws", headers={"origin": "http://testserver"}):
        pass
    assert "user" not in seen


def test_terminal_open_is_audited_with_the_chosen_account(signed_in, db, monkeypatch):
    from sqlalchemy import select
    from app.models import AuditLog

    db.add(Site(name="me", domain="me.example.com", system_user=getpass.getuser(),
                root_dir="/nonexistent-dir", webroot="/nonexistent-dir"))
    db.commit()
    with signed_in.websocket_connect("/terminal/ws?as=me", headers={"origin": "http://testserver"}):
        pass
    row = db.scalars(select(AuditLog).where(AuditLog.action == "terminal.open")).first()
    assert row.target == "me"


def test_site_terminal_buttons_open_as_that_sites_user(signed_in, two_sites, db):
    site = db.scalars(__import__("sqlalchemy").select(Site).where(Site.name == "shop")).first()
    assert "/terminal?as=shop&cd=" in signed_in.get(f"/sites/{site.id}").text
    assert "/terminal?as=shop&cd=" in signed_in.get("/sites").text
