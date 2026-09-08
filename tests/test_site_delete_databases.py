"""Deleting a site must never silently orphan a real MariaDB database.

Regression for a bug where SiteDatabase rows cascade-deleted along with the
site (the panel forgetting the database ever existed) while the actual
database and its MariaDB user were left behind untouched -- recreating the
site and its database later then failed with "Database already exists",
for a database nothing in the panel could see or manage anymore.
"""

from unittest.mock import MagicMock

import pytest

from app.models import Site, SiteDatabase
from app.services import sites as sites_service


class _FakeCtx:
    def __init__(self):
        self.lines = []

    def log(self, line):
        self.lines.append(line)

    def run(self, *_args, **_kwargs):
        return 0


@pytest.fixture(autouse=True)
def fake_system(monkeypatch, tmp_path):
    """Every system-touching call delete_site makes is faked out: no real
    nginx/php/vsftpd/cron/userdel here, per this project's usual split
    between what's unit-testable and what needs the live box."""
    fake_provider = MagicMock()
    fake_provider.installed_versions.return_value = []
    monkeypatch.setattr(sites_service, "get_provider", lambda key: fake_provider)
    monkeypatch.setattr(sites_service.cron_service, "remove_crontab", lambda username: None)
    monkeypatch.setattr(sites_service, "_remove_from_ftp_userlist", lambda username: None)
    return fake_provider


def _site_with_database(db, tmp_path, *, name="blog", domain="blog.example"):
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

    record = SiteDatabase(site_id=site.id, db_name=f"bee_{name}", db_user=f"bee_{name}")
    db.add(record)
    db.flush()
    return site, record


def test_delete_site_keeps_the_database_by_default(db, tmp_path, monkeypatch):
    site, record = _site_with_database(db, tmp_path)
    drop = MagicMock()
    monkeypatch.setattr(sites_service.db_service, "drop_database", drop)

    sites_service.delete_site(db, _FakeCtx(), site, remove_files=False, delete_databases=False)
    db.commit()

    drop.assert_not_called()
    kept = db.get(SiteDatabase, record.id)
    assert kept is not None, "the database's own panel record must survive the site delete"
    assert kept.site_id is None, "it should be detached from the deleted site, not still pointing at it"
    assert kept.db_name == f"bee_blog"


def test_delete_site_can_actually_drop_the_database_when_asked(db, tmp_path, monkeypatch):
    site, record = _site_with_database(db, tmp_path)
    drop = MagicMock()
    monkeypatch.setattr(sites_service.db_service, "drop_database", drop)

    sites_service.delete_site(db, _FakeCtx(), site, remove_files=False, delete_databases=True)
    db.commit()

    drop.assert_called_once_with(record.db_name, record.db_user)
    assert db.get(SiteDatabase, record.id) is None


def test_delete_site_with_no_databases_does_not_touch_db_service(db, tmp_path, monkeypatch):
    root = tmp_path / "empty"
    root.mkdir(parents=True)
    site = Site(
        name="empty",
        domain="empty.example",
        system_user="site_empty",
        root_dir=str(root),
        webroot=str(root),
    )
    db.add(site)
    db.flush()

    drop = MagicMock()
    monkeypatch.setattr(sites_service.db_service, "drop_database", drop)

    sites_service.delete_site(db, _FakeCtx(), site, remove_files=False, delete_databases=True)
    db.commit()

    drop.assert_not_called()
