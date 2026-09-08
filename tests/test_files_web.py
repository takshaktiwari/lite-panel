"""HTTP-level tests for the file manager's new actions.

The service layer (test_files_operations.py) already covers path safety and
zip-slip in depth; these confirm the routes are wired to it correctly --
auth required, CSRF enforced, and a real round trip through each new
endpoint.
"""

import zipfile

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import files as files_service


@pytest.fixture
def sites_root(tmp_path, monkeypatch):
    monkeypatch.setattr(files_service, "settings", type(files_service.settings)(sites_root=tmp_path))
    return tmp_path


@pytest.fixture
def client(db, admin, sites_root):
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


def _csrf(client) -> str:
    page = client.get("/files")
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


# --------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------


def test_files_page_requires_login(client):
    assert client.get("/files").status_code == 303


def test_new_endpoints_require_csrf(signed_in, sites_root):
    (sites_root / "a.txt").write_text("a")
    response = signed_in.post("/files/duplicate", data={"path": ".", "target": "a.txt"})
    assert response.status_code == 400


# --------------------------------------------------------------------------
# Duplicate / archive / extract
# --------------------------------------------------------------------------


def test_duplicate_creates_a_copy(signed_in, sites_root):
    (sites_root / "a.txt").write_text("hello")
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/duplicate", data={"path": ".", "target": "a.txt", "csrf_token": token}
    )
    assert response.status_code == 303
    assert (sites_root / "a-copy.txt").read_text() == "hello"


def test_archive_one_creates_a_zip(signed_in, sites_root):
    (sites_root / "a.txt").write_text("hello")
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/archive", data={"path": ".", "target": "a.txt", "csrf_token": token}
    )
    assert response.status_code == 303
    assert (sites_root / "a.txt.zip").exists()


def test_extract_recreates_the_archived_file(signed_in, sites_root):
    with zipfile.ZipFile(sites_root / "bundle.zip", "w") as zf:
        zf.writestr("hello.txt", "hi")
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/extract", data={"path": ".", "target": "bundle.zip", "csrf_token": token}
    )
    assert response.status_code == 303
    assert (sites_root / "bundle" / "hello.txt").read_text() == "hi"


def test_extract_rejects_zip_slip_end_to_end(signed_in, sites_root):
    """The full route, not just the service function, must refuse this."""
    with zipfile.ZipFile(sites_root / "evil.zip", "w") as zf:
        zf.writestr("../../../etc/evil", "pwned")
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/extract", data={"path": ".", "target": "evil.zip", "csrf_token": token}
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert not (sites_root.parent / "etc").exists()


# --------------------------------------------------------------------------
# Bulk actions
# --------------------------------------------------------------------------


def test_bulk_delete_removes_every_selected_item(signed_in, sites_root):
    (sites_root / "a.txt").write_text("a")
    (sites_root / "b.txt").write_text("b")
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/bulk-delete",
        data={"path": ".", "target": ["a.txt", "b.txt"], "csrf_token": token},
    )
    assert response.status_code == 303
    assert not (sites_root / "a.txt").exists()
    assert not (sites_root / "b.txt").exists()


def test_bulk_delete_with_nothing_selected_is_a_clean_no_op(signed_in, sites_root):
    token = _csrf(signed_in)
    response = signed_in.post(
        "/files/bulk-delete", data={"path": ".", "csrf_token": token}
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_bulk_copy_copies_every_selected_item(signed_in, sites_root):
    (sites_root / "a.txt").write_text("a")
    (sites_root / "b.txt").write_text("b")
    (sites_root / "dest").mkdir()
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/bulk-copy",
        data={
            "path": ".",
            "target": ["a.txt", "b.txt"],
            "destination": "dest",
            "csrf_token": token,
        },
    )
    assert response.status_code == 303
    assert (sites_root / "dest" / "a.txt").read_text() == "a"
    assert (sites_root / "dest" / "b.txt").read_text() == "b"


def test_bulk_copy_rejects_a_traversal_destination(signed_in, sites_root):
    (sites_root / "a.txt").write_text("a")
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/bulk-copy",
        data={
            "path": ".",
            "target": ["a.txt"],
            "destination": "../../etc",
            "csrf_token": token,
        },
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert not (sites_root.parent / "etc" / "a.txt").exists()


def test_bulk_archive_bundles_every_selected_item(signed_in, sites_root):
    (sites_root / "a.txt").write_text("aaa")
    (sites_root / "b.txt").write_text("bbb")
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/bulk-archive",
        data={
            "path": ".",
            "target": ["a.txt", "b.txt"],
            "archive_name": "bundle",
            "csrf_token": token,
        },
    )
    assert response.status_code == 303
    with zipfile.ZipFile(sites_root / "bundle.zip") as zf:
        assert set(zf.namelist()) == {"a.txt", "b.txt"}
