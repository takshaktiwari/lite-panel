"""Unit and integration tests for Lite-Panel self-update feature."""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.jobs import JobStatus
from app.main import create_app
from app.models import AuditLog, Job
from app.services import version as version_service
from app.services.version import VersionInfo, check_for_updates, clear_cache, is_newer_version


@pytest.fixture(autouse=True)
def reset_version_cache():
    clear_cache()
    yield
    clear_cache()


def test_is_newer_version():
    assert is_newer_version("1.0.1", "1.0.0") is True
    assert is_newer_version("v1.1.0", "1.0.9") is True
    assert is_newer_version("2.0.0", "1.9.9") is True
    assert is_newer_version("1.0.0", "1.0.0") is False
    assert is_newer_version("0.9.0", "1.0.0") is False
    assert is_newer_version("v1.0.0", "1.0.0") is False


def test_check_for_updates_cached():
    fake_releases = [
        {
            "tag_name": "1.0.5",
            "name": "v1.0.5 Bug Fixes",
            "body": "Fixed some issues",
            "published_at": "2026-09-08T10:00:00Z",
            "html_url": "https://github.com/takshaktiwari/lite-panel/releases/tag/1.0.5",
        }
    ]

    with patch("app.services.version.fetch_github_releases", return_value=fake_releases) as mock_fetch:
        info1 = check_for_updates()
        assert info1.latest_version == "1.0.5"
        assert info1.update_available is True
        assert info1.release_name == "v1.0.5 Bug Fixes"
        assert mock_fetch.call_count == 1

        # Second call without force should hit memory cache
        info2 = check_for_updates(force=False)
        assert mock_fetch.call_count == 1
        assert info2.latest_version == "1.0.5"

        # Force call should query again
        info3 = check_for_updates(force=True)
        assert mock_fetch.call_count == 2
        assert info3.latest_version == "1.0.5"


def test_check_for_updates_network_failure():
    with patch("app.services.version.fetch_github_releases", side_effect=RuntimeError("connection refused")):
        info = check_for_updates(force=True)
        assert info.error == "connection refused"
        assert info.update_available is False
        assert info.latest_version == version_service.__version__


def test_panel_update_task_handler(tmp_path):
    """Test panel.update job handler invoking git checkout and pip install."""
    from app.tasks import update_panel

    ctx = MagicMock()
    ctx.payload = {"tag": "1.0.0"}

    with patch("app.config.get_settings") as mock_settings, \
         patch("app.shell.run") as mock_run:
        fake_install_dir = tmp_path / "lite-panel"
        fake_install_dir.mkdir()
        (fake_install_dir / ".git").mkdir()
        venv_bin = fake_install_dir / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "pip").touch()
        panel_dir = fake_install_dir / "panel"
        panel_dir.mkdir()
        (panel_dir / "requirements.txt").touch()

        settings_obj = MagicMock()
        settings_obj.install_dir = fake_install_dir
        mock_settings.return_value = settings_obj

        update_panel(ctx)

        # Verified that check ran git fetch, checkout, and pip install
        calls = [call[0][0] for call in ctx.check.call_args_list]
        assert ["git", "fetch", "--tags", "origin"] in calls
        assert ["git", "checkout", "-f", "1.0.0"] in calls
        assert ["git", "reset", "--hard", "1.0.0"] in calls
        assert [str(venv_bin / "pip"), "install", "--quiet", "-r", str(panel_dir / "requirements.txt")] in calls



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


def test_setup_page_shows_update_section(signed_in):
    fake_info = VersionInfo(
        current_version="0.1.0",
        git_commit="abcdef",
        latest_version="1.0.0",
        update_available=True,
        release_name="v1.0.0 Stable",
        release_notes="Brand new release",
        published_at="2026-09-08",
        release_url="https://github.com/...",
        checked_at=123456.0,
    )
    with patch("app.services.version.check_for_updates", return_value=fake_info):
        resp = signed_in.get("/setup")
        assert resp.status_code == 200
        assert "Lite-Panel Updates" in resp.text
        assert "1.0.0" in resp.text
        assert "Update to 1.0.0" in resp.text
        assert "Check for updates" in resp.text


def _extract_csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def test_setup_update_check_route(signed_in):
    with patch("app.services.version.check_for_updates") as mock_check:
        mock_check.return_value = VersionInfo(
            current_version="0.1.0",
            git_commit="abcdef",
            latest_version="1.0.0",
            update_available=True,
            release_name="v1.0.0",
            release_notes="",
            published_at="",
            release_url="",
            checked_at=123.0,
        )
        page = signed_in.get("/setup")
        csrf_token = _extract_csrf(page.text)
        resp = signed_in.post("/setup/update-check", data={"csrf_token": csrf_token})
        assert resp.status_code == 303
        assert "notice=" in resp.headers["location"]
        assert mock_check.called


def test_setup_update_enqueues_job(signed_in, db):
    page = signed_in.get("/setup")
    csrf_token = _extract_csrf(page.text)
    resp = signed_in.post("/setup/update", data={"csrf_token": csrf_token, "tag": "1.0.0"})
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/jobs/")

    job_id = int(resp.headers["location"].split("/")[-1])
    job = db.get(Job, job_id)
    assert job is not None
    assert job.kind == "panel.update"
    assert json.loads(job.payload) == {"tag": "1.0.0"}

