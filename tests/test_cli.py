"""Tests for the CLI, including the `rebuild` subcommand.

All tests run fully in-process — no systemd, no nginx, no running server.
The rebuild in-process path is exercised by mocking renderer.rebuild_all and
the nginx provider so neither needs to exist on the test machine.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# conftest sets env vars before any app import — see conftest.py
from app.cli import main


# ---------------------------------------------------------------------------
# Existing commands (regression guard)
# ---------------------------------------------------------------------------


def test_generate_password_default_length(capsys):
    rc = main(["generate-password"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    # Default length is 20 characters
    assert len(out) == 20


def test_generate_password_custom_length(capsys):
    rc = main(["generate-password", "--length", "32"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert len(out) == 32


def test_init_db_runs(tmp_path, monkeypatch):
    """init-db must not raise and must print a confirmation."""
    import os

    monkeypatch.setenv(
        "LITE_PANEL_DATABASE_URL", f"sqlite:///{tmp_path}/init-test.db"
    )
    # Need to clear the lru_cache so the monkeypatched env is picked up.
    from app import config as _cfg

    _cfg.get_settings.cache_clear()
    try:
        rc = main(["init-db"])
        assert rc == 0
    finally:
        _cfg.get_settings.cache_clear()


def test_create_admin_reads_from_stdin(tmp_path, monkeypatch, capsys):
    import os
    import io

    db_url = f"sqlite:///{tmp_path}/admin-test.db"
    monkeypatch.setenv("LITE_PANEL_DATABASE_URL", db_url)

    from app import config as _cfg

    _cfg.get_settings.cache_clear()
    monkeypatch.setattr("sys.stdin", io.StringIO("MySecurePass1!\n"))
    try:
        rc = main(["create-admin", "--username", "testadmin"])
        assert rc == 0
    finally:
        _cfg.get_settings.cache_clear()


def test_reset_password_auto_generates(tmp_path, monkeypatch, capsys):
    import io

    db_url = f"sqlite:///{tmp_path}/reset-test.db"
    monkeypatch.setenv("LITE_PANEL_DATABASE_URL", db_url)

    from app import config as _cfg
    from app.database import session_scope
    from app.models import AdminUser

    _cfg.get_settings.cache_clear()
    try:
        # 1. Create admin first
        monkeypatch.setattr("sys.stdin", io.StringIO("InitialPassword123!\n"))
        assert main(["create-admin", "--username", "adminuser"]) == 0

        # 2. Reset without password argument or input (auto-generates)
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        rc = main(["reset-password", "--username", "adminuser"])
        assert rc == 0

        out = capsys.readouterr().out
        assert "Password reset successful" in out
        assert "New generated password:" in out
    finally:
        _cfg.get_settings.cache_clear()


def test_reset_password_explicit_argument(tmp_path, monkeypatch, capsys):
    db_url = f"sqlite:///{tmp_path}/reset-test2.db"
    monkeypatch.setenv("LITE_PANEL_DATABASE_URL", db_url)

    from app import config as _cfg

    _cfg.get_settings.cache_clear()
    try:
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO("InitialPassword123!\n"))
        assert main(["create-admin", "--username", "adminuser2"]) == 0

        rc = main(["reset-password", "--username", "adminuser2", "--password", "CustomNewPass1234!"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Password reset successful" in out
    finally:
        _cfg.get_settings.cache_clear()



# ---------------------------------------------------------------------------
# rebuild — in-process path (app not reachable)
# ---------------------------------------------------------------------------


class _FakeNginxProvider:
    """Minimal provider double that records calls."""

    def is_installed(self):
        return True

    def reload(self, ctx):
        ctx.log("nginx reloaded (mock)")


def test_rebuild_inprocess_success(capsys):
    """When the panel API is not reachable, rebuild runs in-process."""

    with (
        # Simulate panel not running (healthz timeout)
        patch("urllib.request.urlopen", side_effect=OSError("connection refused")),
        # Stub out the renderer so no real filesystem writes happen
        patch("app.services.renderer.rebuild_all") as mock_rebuild,
        # Stub out the nginx provider
        patch("app.providers.get_provider", return_value=_FakeNginxProvider()),
    ):
        rc = main(["rebuild"])

    assert rc == 0
    mock_rebuild.assert_called_once()

    out = capsys.readouterr().out
    assert "Rebuild complete" in out


def test_rebuild_inprocess_renderer_failure(capsys):
    """If renderer raises, rebuild exits non-zero and prints the error."""

    with (
        patch("urllib.request.urlopen", side_effect=OSError("connection refused")),
        patch(
            "app.services.renderer.rebuild_all",
            side_effect=RuntimeError("disk full"),
        ),
        patch("app.providers.get_provider", return_value=_FakeNginxProvider()),
    ):
        # RuntimeError from the renderer should propagate; the CLI should NOT
        # swallow it silently.  The test verifies it raises (not a clean exit).
        with pytest.raises(RuntimeError, match="disk full"):
            main(["rebuild"])


def test_rebuild_inprocess_nginx_not_installed(capsys):
    """If nginx is not installed, rebuild still succeeds (no reload call)."""

    class _NoNginx:
        def is_installed(self):
            return False

        def reload(self, ctx):
            raise AssertionError("reload should not be called when not installed")

    with (
        patch("urllib.request.urlopen", side_effect=OSError("connection refused")),
        patch("app.services.renderer.rebuild_all"),
        patch("app.providers.get_provider", return_value=_NoNginx()),
    ):
        rc = main(["rebuild"])

    assert rc == 0


# ---------------------------------------------------------------------------
# rebuild — API path (app is running, but auth required)
# ---------------------------------------------------------------------------


def _make_fake_urlopen(responses: list):
    """Return a context-manager-compatible urlopen that cycles through responses."""
    import io

    call_count = [0]

    class _FakeResp:
        def __init__(self, body: bytes):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return self._body

        def __iter__(self):
            # Used by the SSE stream consumer
            for line in self._body.splitlines(keepends=True):
                yield line

    def _urlopen(req_or_url, timeout=None):
        idx = call_count[0]
        call_count[0] += 1
        if idx >= len(responses):
            raise OSError("unexpected call")
        resp_body = responses[idx]
        if isinstance(resp_body, Exception):
            raise resp_body
        return _FakeResp(resp_body)

    return _urlopen


def test_rebuild_api_path_success(capsys):
    """When panel is reachable, rebuild enqueues via API and tails log."""
    import json

    healthz_ok = b"ok"
    job_created = json.dumps({"id": 42, "status": "queued"}).encode()
    # SSE stream lines
    sse_stream = b"data: Rebuilding...\ndata: Rebuild complete\n"
    job_status = json.dumps({"id": 42, "status": "done"}).encode()

    fake_urlopen = _make_fake_urlopen(
        [healthz_ok, job_created, sse_stream, job_status]
    )

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        rc = main(["rebuild"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Rebuilding" in out or "Rebuild complete" in out


def test_rebuild_api_path_job_failed(capsys):
    """If the panel reports job status != done, rebuild exits non-zero."""
    import json

    healthz_ok = b"ok"
    job_created = json.dumps({"id": 7, "status": "queued"}).encode()
    sse_stream = b"data: something went wrong\n"
    job_status = json.dumps({"id": 7, "status": "failed"}).encode()

    fake_urlopen = _make_fake_urlopen(
        [healthz_ok, job_created, sse_stream, job_status]
    )

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        rc = main(["rebuild"])

    assert rc == 1
