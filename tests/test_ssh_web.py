"""HTTP-level tests for the SSH router."""

from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Site, SshKey
from app.services import ssh_keys as ssh_service


VALID_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGo4D8m1R7Z8DlhXq3qQ9k6+K9nK+qV1pZfVvJk11111 test@example.com"


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


def _csrf(client) -> str:
    page = client.get("/ssh")
    marker = 'name="csrf_token" value="'
    start = page.text.find(marker)
    assert start != -1
    start += len(marker)
    end = page.text.find('"', start)
    return page.text[start:end]


def test_ssh_list_requires_login(client):
    res = client.get("/ssh")
    assert res.status_code == 303
    assert res.headers["location"] == "/login"


def test_ssh_list_renders(signed_in):
    res = signed_in.get("/ssh")
    assert res.status_code == 200
    assert "SSH Access" in res.text
    assert "Generate SSH key pair" in res.text
    assert "Import existing public key" in res.text


def test_ssh_generate_downloads_pem(signed_in, db, monkeypatch, tmp_path):
    monkeypatch.setattr(ssh_service, "home_dir_for_user", lambda u, db=None: tmp_path)
    monkeypatch.setattr(ssh_service, "set_user_shell", lambda u, s: None)
    fake_run = MagicMock()
    fake_run.stdout = "256 SHA256:genfp comment (ED25519)"
    monkeypatch.setattr(ssh_service, "run", lambda *a, **k: fake_run)
    monkeypatch.setattr(
        ssh_service,
        "generate_key_pair",
        lambda comment, key_type: ("-----BEGIN OPENSSH PRIVATE KEY-----\nMOCK\n-----END OPENSSH PRIVATE KEY-----", VALID_PUB, "SHA256:genfp"),
    )

    csrf = _csrf(signed_in)
    res = signed_in.post(
        "/ssh/generate",
        data={
            "csrf_token": csrf,
            "name": "My New Laptop",
            "target_user": "root",
            "key_type": "ed25519",
        },
    )

    assert res.status_code == 200
    assert res.headers["content-type"] == "application/x-pem-file"
    assert "My_New_Laptop_root.pem" in res.headers["content-disposition"]
    assert "BEGIN OPENSSH PRIVATE KEY" in res.text

    key = db.scalar(select(SshKey).where(SshKey.name == "My New Laptop"))
    assert key is not None
    assert key.system_user == "root"
    assert key.fingerprint == "SHA256:genfp"


def test_ssh_import_creates_key(signed_in, db, monkeypatch, tmp_path):
    monkeypatch.setattr(ssh_service, "home_dir_for_user", lambda u, db=None: tmp_path)
    monkeypatch.setattr(ssh_service, "set_user_shell", lambda u, s: None)
    monkeypatch.setattr(ssh_service, "run", lambda *a, **k: None)
    monkeypatch.setattr(
        ssh_service,
        "parse_public_key",
        lambda pk: ("SHA256:importfp", "ssh-ed25519"),
    )

    csrf = _csrf(signed_in)
    res = signed_in.post(
        "/ssh/import",
        data={
            "csrf_token": csrf,
            "name": "Desktop Workstation",
            "target_user": "root",
            "public_key": VALID_PUB,
        },
    )

    assert res.status_code == 303
    assert "/ssh" in res.headers["location"]

    key = db.scalar(select(SshKey).where(SshKey.name == "Desktop Workstation"))
    assert key is not None
    assert key.fingerprint == "SHA256:importfp"


def test_ssh_get_public_key(signed_in, db):
    key = SshKey(
        name="Key View",
        system_user="root",
        public_key=VALID_PUB,
        fingerprint="SHA256:viewfp",
        key_type="ssh-ed25519",
    )
    db.add(key)
    db.commit()

    res = signed_in.get(f"/ssh/{key.id}/public-key")
    assert res.status_code == 200
    assert res.text == VALID_PUB


def test_ssh_delete_key(signed_in, db, monkeypatch, tmp_path):
    monkeypatch.setattr(ssh_service, "home_dir_for_user", lambda u, db=None: tmp_path)
    monkeypatch.setattr(ssh_service, "set_user_shell", lambda u, s: None)
    monkeypatch.setattr(ssh_service, "run", lambda *a, **k: None)

    key = SshKey(
        name="Key To Delete",
        system_user="root",
        public_key=VALID_PUB,
        fingerprint="SHA256:deletefp",
        key_type="ssh-ed25519",
    )
    db.add(key)
    db.commit()
    key_id = key.id

    csrf = _csrf(signed_in)
    res = signed_in.post(
        f"/ssh/{key_id}/delete",
        data={"csrf_token": csrf},
    )

    assert res.status_code == 303
    assert "/ssh" in res.headers["location"]
    db.expire_all()
    assert db.get(SshKey, key_id) is None
