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
# Permissions
# --------------------------------------------------------------------------


def test_chmod_updates_the_permission_bits(signed_in, sites_root):
    import stat

    (sites_root / "a.txt").write_text("a")
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/chmod",
        data={"path": ".", "target": "a.txt", "mode": "600", "csrf_token": token},
    )
    assert response.status_code == 303
    assert stat.S_IMODE((sites_root / "a.txt").stat().st_mode) == 0o600


def test_chmod_rejects_an_invalid_mode(signed_in, sites_root):
    (sites_root / "a.txt").write_text("a")
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/chmod",
        data={"path": ".", "target": "a.txt", "mode": "not-a-mode", "csrf_token": token},
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_bulk_chmod_updates_every_selected_item(signed_in, sites_root):
    import stat

    (sites_root / "a.txt").write_text("a")
    (sites_root / "b.txt").write_text("b")
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/bulk-chmod",
        data={"path": ".", "target": ["a.txt", "b.txt"], "mode": "600", "csrf_token": token},
    )
    assert response.status_code == 303
    assert stat.S_IMODE((sites_root / "a.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((sites_root / "b.txt").stat().st_mode) == 0o600


def test_bulk_chmod_with_nothing_selected_is_a_clean_no_op(signed_in, sites_root):
    token = _csrf(signed_in)
    response = signed_in.post(
        "/files/bulk-chmod", data={"path": ".", "mode": "644", "csrf_token": token}
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_chmod_recursive_updates_files_inside_a_folder(signed_in, sites_root):
    import stat

    (sites_root / "app" / "nested").mkdir(parents=True)
    (sites_root / "app" / "file.txt").write_text("x")
    (sites_root / "app" / "nested" / "inner.txt").write_text("y")
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/chmod-recursive",
        data={
            "path": ".",
            "target": "app",
            "mode": "600",
            "scope": "files",
            "csrf_token": token,
        },
    )
    assert response.status_code == 303
    assert "notice=" in response.headers["location"]
    assert stat.S_IMODE((sites_root / "app" / "file.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((sites_root / "app" / "nested" / "inner.txt").stat().st_mode) == 0o600


def test_chmod_recursive_rejects_a_file_target(signed_in, sites_root):
    (sites_root / "a.txt").write_text("a")
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/chmod-recursive",
        data={"path": ".", "target": "a.txt", "mode": "644", "scope": "files", "csrf_token": token},
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


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


def test_move_relocates_a_single_item(signed_in, sites_root):
    (sites_root / "a.txt").write_text("hello")
    (sites_root / "dest").mkdir()
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/move",
        data={"path": ".", "target": "a.txt", "destination": "dest", "csrf_token": token},
    )
    assert response.status_code == 303
    assert (sites_root / "dest" / "a.txt").read_text() == "hello"
    assert not (sites_root / "a.txt").exists()


def test_bulk_move_relocates_every_selected_item(signed_in, sites_root):
    (sites_root / "a.txt").write_text("a")
    (sites_root / "b.txt").write_text("b")
    (sites_root / "dest").mkdir()
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/bulk-move",
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
    assert not (sites_root / "a.txt").exists()
    assert not (sites_root / "b.txt").exists()


def test_bulk_move_rejects_a_traversal_destination(signed_in, sites_root):
    (sites_root / "a.txt").write_text("a")
    token = _csrf(signed_in)

    response = signed_in.post(
        "/files/bulk-move",
        data={
            "path": ".",
            "target": ["a.txt"],
            "destination": "../../etc",
            "csrf_token": token,
        },
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert (sites_root / "a.txt").exists()  # nothing moved on failure


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


# --------------------------------------------------------------------------
# Chunked upload
# --------------------------------------------------------------------------


def test_chunk_upload_round_trip(signed_in, sites_root):
    token = _csrf(signed_in)

    init_response = signed_in.post(
        "/files/chunk-upload/init",
        json={"path": ".", "filename": "a.txt", "size": 5},
        headers={"X-CSRF-Token": token},
    )
    assert init_response.status_code == 200
    upload_id = init_response.json()["upload_id"]

    chunk_response = signed_in.post(
        f"/files/chunk-upload/{upload_id}/chunk",
        data={"index": "0", "csrf_token": token},
        files={"chunk": ("chunk", b"hello")},
    )
    assert chunk_response.status_code == 200
    assert chunk_response.json() == {"received_bytes": 5}

    complete_response = signed_in.post(
        f"/files/chunk-upload/{upload_id}/complete",
        data={"csrf_token": token},
    )
    assert complete_response.status_code == 200
    assert complete_response.json() == {"name": "a.txt"}
    assert (sites_root / "a.txt").read_bytes() == b"hello"


def test_chunk_upload_init_requires_csrf(signed_in, sites_root):
    response = signed_in.post(
        "/files/chunk-upload/init",
        json={"path": ".", "filename": "a.txt", "size": 5},
    )
    assert response.status_code == 400


def test_chunk_upload_abort_discards_the_session(signed_in, sites_root):
    token = _csrf(signed_in)
    init_response = signed_in.post(
        "/files/chunk-upload/init",
        json={"path": ".", "filename": "a.txt", "size": 5},
        headers={"X-CSRF-Token": token},
    )
    upload_id = init_response.json()["upload_id"]

    abort_response = signed_in.post(
        f"/files/chunk-upload/{upload_id}/abort",
        data={"csrf_token": token},
    )
    assert abort_response.status_code == 200

    complete_response = signed_in.post(
        f"/files/chunk-upload/{upload_id}/complete",
        data={"csrf_token": token},
    )
    assert complete_response.status_code == 400


def test_chunk_upload_rejects_a_traversal_filename(signed_in, sites_root):
    token = _csrf(signed_in)
    response = signed_in.post(
        "/files/chunk-upload/init",
        json={"path": ".", "filename": "../../etc/evil", "size": 5},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 400
