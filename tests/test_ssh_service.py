"""Unit tests for the SSH Access keys service (app.services.ssh_keys)."""

import os
from unittest.mock import MagicMock

import pytest

from app.models import Site, SshKey
from app.services import ssh_keys as ssh_service
from app.validators import ValidationError


# Sample valid OpenSSH public keys for testing
VALID_ED25519_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGo4D8m1R7Z8DlhXq3qQ9k6+K9nK+qV1pZfVvJk11111 test@example.com"
VALID_RSA_PUB = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC1test12345 user@workstation"


def test_parse_public_key_empty():
    with pytest.raises(ValidationError, match="Public key cannot be empty"):
        ssh_service.parse_public_key("")


def test_parse_public_key_invalid_format():
    with pytest.raises(ValidationError, match="Invalid public key format"):
        ssh_service.parse_public_key("singleword")


def test_parse_public_key_unsupported_type():
    with pytest.raises(ValidationError, match="Unsupported key type"):
        ssh_service.parse_public_key("dsa-key AAAAB3NzaC1kc3MA... test")


def test_parse_public_key_valid(monkeypatch):
    fake_run = MagicMock()
    fake_run.stdout = "256 SHA256:abc123mockfingerprint test@example.com (ED25519)"
    monkeypatch.setattr(ssh_service, "run", lambda *a, **k: fake_run)
    monkeypatch.setattr(ssh_service, "is_available", lambda: True)

    fingerprint, key_type = ssh_service.parse_public_key(VALID_ED25519_PUB)
    assert fingerprint == "SHA256:abc123mockfingerprint"
    assert key_type == "ssh-ed25519"


def test_render_authorized_keys():
    k1 = SshKey(name="k1", system_user="root", public_key=VALID_ED25519_PUB, fingerprint="fp1", key_type="ssh-ed25519")
    k2 = SshKey(name="k2", system_user="root", public_key=VALID_RSA_PUB, fingerprint="fp2", key_type="ssh-rsa")

    rendered = ssh_service.render_authorized_keys([k1, k2])
    assert ssh_service.MARKER in rendered
    assert VALID_ED25519_PUB in rendered
    assert VALID_RSA_PUB in rendered


def test_sync_authorized_keys_writes_file_and_permissions(tmp_path, monkeypatch):
    home = tmp_path / "home_user"
    home.mkdir()

    k1 = SshKey(name="k1", system_user="site_blog", public_key=VALID_ED25519_PUB, fingerprint="fp1", key_type="ssh-ed25519")

    shells_set = []
    monkeypatch.setattr(ssh_service, "set_user_shell", lambda u, s: shells_set.append((u, s)))
    monkeypatch.setattr(ssh_service, "run", lambda *a, **k: None)

    ssh_service.sync_authorized_keys("site_blog", [k1], home_dir=home)

    auth_file = home / ".ssh" / "authorized_keys"
    assert auth_file.is_file()
    assert VALID_ED25519_PUB in auth_file.read_text(encoding="utf-8")
    assert ("site_blog", ssh_service.LOGIN_SHELL) in shells_set


def test_sync_authorized_keys_cleans_up_when_no_keys(tmp_path, monkeypatch):
    home = tmp_path / "home_user"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    auth_file = ssh_dir / "authorized_keys"
    auth_file.write_text("something")

    shells_set = []
    monkeypatch.setattr(ssh_service, "set_user_shell", lambda u, s: shells_set.append((u, s)))

    ssh_service.sync_authorized_keys("site_blog", [], home_dir=home)

    assert not auth_file.exists()
    assert ("site_blog", "/usr/sbin/nologin") in shells_set


def test_create_key_and_duplicate_rejection(db, tmp_path, monkeypatch):
    monkeypatch.setattr(ssh_service, "home_dir_for_user", lambda u, db=None: tmp_path)
    monkeypatch.setattr(ssh_service, "set_user_shell", lambda u, s: None)
    monkeypatch.setattr(ssh_service, "run", lambda *a, **k: None)
    monkeypatch.setattr(ssh_service, "parse_public_key", lambda pk: ("SHA256:fixedfp", "ssh-ed25519"))

    key = ssh_service.create_key(
        db,
        name="Test Key",
        system_user="root",
        public_key=VALID_ED25519_PUB,
    )

    assert key.id is not None
    assert key.fingerprint == "SHA256:fixedfp"

    # Duplicate should fail
    with pytest.raises(ValidationError, match="already registered"):
        ssh_service.create_key(
            db,
            name="Another Key",
            system_user="root",
            public_key=VALID_ED25519_PUB,
        )


def test_delete_key(db, tmp_path, monkeypatch):
    monkeypatch.setattr(ssh_service, "home_dir_for_user", lambda u, db=None: tmp_path)
    monkeypatch.setattr(ssh_service, "set_user_shell", lambda u, s: None)
    monkeypatch.setattr(ssh_service, "run", lambda *a, **k: None)
    monkeypatch.setattr(ssh_service, "parse_public_key", lambda pk: ("SHA256:fp-to-delete", "ssh-ed25519"))

    key = ssh_service.create_key(
        db,
        name="Key To Delete",
        system_user="root",
        public_key=VALID_ED25519_PUB,
    )

    assert db.get(SshKey, key.id) is not None
    ssh_service.delete_key(db, key)
    assert db.get(SshKey, key.id) is None
