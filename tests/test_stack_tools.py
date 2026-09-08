"""The "Common tools" section of the Stack page: git, Composer, Redis,
unzip, and Node.js (the one with a version picker, since NodeSource only
ever serves one major release line system-wide).

The job worker is never started here (see test_cron_web.py's isolated_worker_queue
fixture, reused for the same reason) -- these confirm routing, validation and
provider registration, not that a real ``apt-get`` run succeeds.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import jobs
from app.main import create_app
from app.providers import all_providers, get_provider
from app.providers.nodejs import NodeProvider
from app.validators import ValidationError, validate_node_major

# --------------------------------------------------------------------------
# validate_node_major
# --------------------------------------------------------------------------


def test_validate_node_major_accepts_a_plain_major():
    assert validate_node_major("20") == "20"
    assert validate_node_major("8") == "8"


@pytest.mark.parametrize("value", ["20.11", "v20", "20; rm -rf /", "", "0", "-1", "100"])
def test_validate_node_major_rejects_anything_else(value):
    with pytest.raises(ValidationError):
        validate_node_major(value)


# --------------------------------------------------------------------------
# Provider registration
# --------------------------------------------------------------------------


def test_new_tool_providers_are_registered_under_the_tools_category():
    for key in ("git", "composer", "redis", "unzip", "nodejs"):
        provider = get_provider(key)
        assert provider.category == "tools"


def test_infra_providers_are_unaffected():
    for key in ("nginx", "php", "mariadb", "certbot", "vsftpd"):
        assert get_provider(key).category == "infra"


def test_all_providers_have_a_description_for_the_stack_page():
    for provider in all_providers():
        assert provider.description


# --------------------------------------------------------------------------
# NodeProvider
# --------------------------------------------------------------------------


def _node():
    return NodeProvider()


def test_installed_versions_reports_only_the_major():
    provider = _node()
    with patch("app.services.apt.installed_version", return_value="20.11.1-1nodesource1"):
        assert provider.installed_versions() == ["20"]


def test_installed_versions_is_empty_when_nodejs_is_not_installed():
    provider = _node()
    with patch("app.services.apt.installed_version", return_value=None):
        assert provider.installed_versions() == []


def test_available_versions_filters_by_live_probe():
    provider = _node()
    with patch("app.services.apt.apt_available", return_value=True), \
         patch("app.providers.nodejs.is_major_supported", side_effect=lambda m: m == "20"):
        assert provider.available_versions() == ["20"]


def test_install_refuses_a_version_not_available():
    provider = _node()
    with patch.object(provider, "is_version_installed", return_value=False), \
         patch.object(provider, "available_versions", return_value=["20"]):
        with pytest.raises(ValidationError) as exc:
            provider.install(_FakeCtx(), "18")
        assert "18" in str(exc.value)
        assert "20" in str(exc.value)


def test_install_requires_a_version():
    provider = _node()
    with pytest.raises(ValidationError):
        provider.install(_FakeCtx(), None)


def test_install_does_nothing_when_already_on_that_major():
    provider = _node()
    ctx = _FakeCtx()
    with patch.object(provider, "is_version_installed", return_value=True), \
         patch.object(provider, "_add_repository") as add_repo:
        provider.install(ctx, "20")
        add_repo.assert_not_called()


def test_install_adds_the_repository_and_installs_the_package():
    provider = _node()
    ctx = _FakeCtx()
    with patch.object(provider, "is_version_installed", return_value=False), \
         patch.object(provider, "available_versions", return_value=["20"]), \
         patch.object(provider, "installed_versions", return_value=[]), \
         patch.object(provider, "_add_repository") as add_repo, \
         patch("app.providers.nodejs.apt.install") as apt_install:
        provider.install(ctx, "20")
        add_repo.assert_called_once_with(ctx, "20")
        apt_install.assert_called_once_with(ctx, ["nodejs"])


def test_uninstall_removes_the_package_and_source_list(tmp_path):
    provider = _node()
    ctx = _FakeCtx()
    fake_source = tmp_path / "nodesource.list"
    fake_source.write_text("deb ...\n")

    with patch("app.providers.nodejs.SOURCE_LIST", fake_source), \
         patch("app.providers.nodejs.apt.remove") as apt_remove:
        provider.uninstall(ctx)
        apt_remove.assert_called_once_with(ctx, ["nodejs"], purge=True)
        assert not fake_source.exists()


class _FakeCtx:
    def log(self, *_args, **_kwargs):
        pass

    def check(self, *_args, **_kwargs):
        pass

    def run(self, *_args, **_kwargs):
        return 0


# --------------------------------------------------------------------------
# Router / page
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_worker_queue(monkeypatch):
    """See test_cron_web.py -- the worker singleton is shared across test
    files in this process, so submission is a no-op here."""
    monkeypatch.setattr(jobs.worker, "submit", lambda job_id: None)


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


def test_stack_page_lists_common_tools(signed_in):
    page = signed_in.get("/stack")
    assert page.status_code == 200
    for name in ("Git", "Composer", "Redis", "unzip", "Node.js"):
        assert name in page.text


def _csrf(client) -> str:
    page = client.get("/stack")
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


def test_install_a_simple_tool_enqueues_a_job(signed_in):
    token = _csrf(signed_in)
    response = signed_in.post(
        "/stack/install", data={"key": "git", "csrf_token": token}
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")


def test_install_unknown_component_is_rejected(signed_in):
    token = _csrf(signed_in)
    response = signed_in.post(
        "/stack/install", data={"key": "not-a-real-provider", "csrf_token": token}
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_install_nodejs_without_a_version_is_rejected(signed_in):
    token = _csrf(signed_in)
    response = signed_in.post(
        "/stack/install", data={"key": "nodejs", "csrf_token": token}
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_install_nodejs_with_a_malformed_version_is_rejected(signed_in):
    token = _csrf(signed_in)
    response = signed_in.post(
        "/stack/install",
        data={"key": "nodejs", "version": "20; rm -rf /", "csrf_token": token},
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_install_nodejs_with_a_valid_version_enqueues_a_job(signed_in):
    token = _csrf(signed_in)
    with patch("app.providers.nodejs.NodeProvider.available_versions", return_value=["20"]):
        response = signed_in.post(
            "/stack/install",
            data={"key": "nodejs", "version": "20", "csrf_token": token},
        )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")
