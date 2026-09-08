"""FTP accounts: any number, each scoped to its own folder.

The core guarantee under test: deleting an FTP account never deletes, nor
even touches, the folder it pointed at -- whether that folder is a site's
own webroot (kept for PHP-FPM) or a dedicated account's own folder (kept
because destroying an operator's files on account deletion would be
needlessly destructive and impossible to undo).
"""

import os
from unittest.mock import MagicMock

import pytest

from app.models import FtpAccount, Site
from app.services import ftp as ftp_service
from app.validators import ValidationError


@pytest.fixture(autouse=True)
def sites_root(tmp_path, monkeypatch):
    """Point the panel's sites root at a scratch directory, and make sure
    every system-touching call in this module is faked out: none of these
    tests may run useradd/chown/chpasswd/passwd for real, or write to
    /etc/vsftpd.userlist."""
    from app.services import sites as sites_service

    monkeypatch.setattr(sites_service, "sites_root", lambda: tmp_path)

    fake_vsftpd = MagicMock()
    fake_vsftpd.is_installed.return_value = True
    monkeypatch.setattr(ftp_service, "get_provider", lambda key: fake_vsftpd)

    monkeypatch.setattr(sites_service, "add_to_ftp_userlist", lambda username: None)
    monkeypatch.setattr(sites_service, "_remove_from_ftp_userlist", lambda username: None)
    monkeypatch.setattr(sites_service, "_user_exists", lambda username: False)
    monkeypatch.setattr(ftp_service, "run", lambda *a, **k: None)
    monkeypatch.setattr(ftp_service, "ensure_shell_allowed", lambda shell: None)

    return tmp_path


class _FakeCtx:
    def __init__(self):
        self.checked = []
        self.ran = []

    def log(self, *_args, **_kwargs):
        pass

    def check(self, args, **_kwargs):
        self.checked.append(args)

    def run(self, args, **_kwargs):
        self.ran.append(args)
        return 0


def _site(db, tmp_path, name="moly", domain="moly.example"):
    root = tmp_path / name
    root.mkdir(parents=True)
    site = Site(
        name=name,
        domain=domain,
        system_user=f"site_{name}",
        root_dir=str(root),
        webroot=str(root),
    )
    db.add(site)
    db.flush()
    return site


# --------------------------------------------------------------------------
# resolve_path / conflicts
# --------------------------------------------------------------------------


def test_resolve_path_stays_within_sites_root(sites_root):
    resolved = ftp_service.resolve_path(str(sites_root / "app"))
    assert resolved == sites_root / "app"


def test_resolve_path_rejects_traversal(sites_root):
    with pytest.raises(ValidationError):
        ftp_service.resolve_path(str(sites_root.parent / "etc" / "passwd"))


def test_create_account_refuses_the_whole_sites_root(db, sites_root):
    with pytest.raises(ValidationError):
        ftp_service.create_account(
            db, _FakeCtx(), username="bob", password="x", path=str(sites_root)
        )


# --------------------------------------------------------------------------
# Site-linked accounts
# --------------------------------------------------------------------------


def test_create_account_on_a_sites_root_reuses_the_sites_own_user(db, sites_root):
    site = _site(db, sites_root)

    account = ftp_service.create_account(
        db, _FakeCtx(), username="whatever-i-typed", password="x", path=site.root_dir
    )

    assert account.username == site.system_user
    assert account.site_id == site.id
    assert account.home_dir == site.root_dir


def test_create_account_on_a_site_does_not_chown_anything(db, sites_root, monkeypatch):
    """Reusing the site's own account touches no ownership -- it's already
    correct."""
    calls = []
    monkeypatch.setattr(ftp_service, "run", lambda args, **k: calls.append(args))
    site = _site(db, sites_root)

    ftp_service.create_account(db, _FakeCtx(), username="x", password="x", path=site.root_dir)

    assert not any(args[0] == "chown" for args in calls)


def test_second_account_on_the_same_site_root_is_refused(db, sites_root):
    site = _site(db, sites_root)
    ftp_service.create_account(db, _FakeCtx(), username="a", password="x", path=site.root_dir)

    with pytest.raises(ValidationError):
        ftp_service.create_account(db, _FakeCtx(), username="b", password="y", path=site.root_dir)


# --------------------------------------------------------------------------
# Dedicated accounts
# --------------------------------------------------------------------------


def test_create_dedicated_account_for_a_custom_path(db, sites_root):
    path = sites_root / "shared-uploads"

    account = ftp_service.create_account(
        db, _FakeCtx(), username="client-bob", password="x", path=str(path)
    )

    assert account.username == "client-bob"
    assert account.site_id is None
    assert account.home_dir == str(path)
    assert path.is_dir()  # created for the account


def test_create_dedicated_account_chowns_the_folder(db, sites_root, monkeypatch):
    calls = []
    monkeypatch.setattr(ftp_service, "run", lambda args, **k: calls.append(args))
    path = sites_root / "uploads"

    ftp_service.create_account(db, _FakeCtx(), username="bob", password="x", path=str(path))

    chowns = [c for c in calls if c[0] == "chown"]
    assert chowns and chowns[0][1] == "-R"
    assert chowns[0][2] == "bob:bob"


def test_create_dedicated_account_runs_useradd(db, sites_root):
    ctx = _FakeCtx()
    path = sites_root / "uploads"

    ftp_service.create_account(db, ctx, username="bob", password="x", path=str(path))

    useradd_calls = [c for c in ctx.checked if c[0] == "useradd"]
    assert len(useradd_calls) == 1
    assert "bob" in useradd_calls[0]
    assert str(path) in useradd_calls[0]


def test_dedicated_username_reserved_prefix_is_refused(db, sites_root):
    with pytest.raises(ValidationError):
        ftp_service.create_account(
            db, _FakeCtx(), username="site_anything", password="x",
            path=str(sites_root / "custom"),
        )


def test_dedicated_username_colliding_with_a_site_is_refused(db, sites_root):
    site = _site(db, sites_root)
    with pytest.raises(ValidationError):
        ftp_service.create_account(
            db, _FakeCtx(), username=site.system_user, password="x",
            path=str(sites_root / "custom"),
        )


def test_dedicated_username_collision_with_another_ftp_account_is_refused(db, sites_root):
    ftp_service.create_account(
        db, _FakeCtx(), username="bob", password="x", path=str(sites_root / "a")
    )
    with pytest.raises(ValidationError):
        ftp_service.create_account(
            db, _FakeCtx(), username="bob", password="y", path=str(sites_root / "b")
        )


def test_second_account_nested_inside_an_existing_ones_folder_is_refused(db, sites_root):
    ftp_service.create_account(
        db, _FakeCtx(), username="a", password="x", path=str(sites_root / "app")
    )
    with pytest.raises(ValidationError):
        ftp_service.create_account(
            db, _FakeCtx(), username="b", password="y", path=str(sites_root / "app" / "sub")
        )


def test_second_account_that_is_an_ancestor_of_an_existing_ones_folder_is_refused(db, sites_root):
    ftp_service.create_account(
        db, _FakeCtx(), username="a", password="x", path=str(sites_root / "app" / "sub")
    )
    with pytest.raises(ValidationError):
        ftp_service.create_account(
            db, _FakeCtx(), username="b", password="y", path=str(sites_root / "app")
        )


def test_create_account_requires_a_password(db, sites_root):
    with pytest.raises(ValidationError):
        ftp_service.create_account(
            db, _FakeCtx(), username="bob", password="", path=str(sites_root / "custom")
        )


def test_create_account_refuses_when_vsftpd_not_installed(db, sites_root, monkeypatch):
    not_installed = MagicMock()
    not_installed.is_installed.return_value = False
    monkeypatch.setattr(ftp_service, "get_provider", lambda key: not_installed)

    with pytest.raises(ValidationError):
        ftp_service.create_account(
            db, _FakeCtx(), username="bob", password="x", path=str(sites_root / "custom")
        )


# --------------------------------------------------------------------------
# Deletion -- the folder must always survive
# --------------------------------------------------------------------------


def test_deleting_a_dedicated_account_never_touches_its_folder(db, sites_root):
    path = sites_root / "uploads"
    account = ftp_service.create_account(
        db, _FakeCtx(), username="bob", password="x", path=str(path)
    )
    marker = path / "keep-me.txt"
    marker.write_text("precious data")

    ftp_service.delete_account(db, _FakeCtx(), account)

    assert path.is_dir()
    assert marker.read_text() == "precious data"
    assert db.get(FtpAccount, account.id) is None


def test_deleting_a_dedicated_account_removes_the_system_user_without_remove_flag(db, sites_root):
    path = sites_root / "uploads"
    account = ftp_service.create_account(
        db, _FakeCtx(), username="bob", password="x", path=str(path)
    )
    ctx = _FakeCtx()

    ftp_service.delete_account(db, ctx, account)

    userdel_calls = [c for c in ctx.ran if c[0] == "userdel"]
    assert userdel_calls == [["userdel", "bob"]]  # never "--remove"


def test_deleting_a_site_linked_account_never_touches_the_sites_folder(db, sites_root):
    site = _site(db, sites_root)
    marker = os.path.join(site.root_dir, "index.php")
    with open(marker, "w") as f:
        f.write("<?php echo 1;")

    account = ftp_service.create_account(
        db, _FakeCtx(), username="ignored", password="x", path=site.root_dir
    )

    ftp_service.delete_account(db, _FakeCtx(), account)

    assert os.path.isdir(site.root_dir)
    assert open(marker).read() == "<?php echo 1;"
    assert db.get(FtpAccount, account.id) is None


def test_deleting_a_site_linked_account_does_not_userdel_the_sites_account(db, sites_root):
    site = _site(db, sites_root)
    account = ftp_service.create_account(
        db, _FakeCtx(), username="ignored", password="x", path=site.root_dir
    )
    ctx = _FakeCtx()

    ftp_service.delete_account(db, ctx, account)

    assert not any(c[0] == "userdel" for c in ctx.ran)


def test_deleting_a_site_linked_account_locks_the_password(db, sites_root, monkeypatch):
    calls = []
    monkeypatch.setattr(ftp_service, "run", lambda args, **k: calls.append(args))
    site = _site(db, sites_root)
    account = ftp_service.create_account(
        db, _FakeCtx(), username="ignored", password="x", path=site.root_dir
    )
    calls.clear()

    ftp_service.delete_account(db, _FakeCtx(), account)

    assert ["passwd", "--lock", site.system_user] in calls
