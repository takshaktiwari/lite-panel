"""Tests for the redesigned backup service, scheduler, and web interface."""

import json
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.database import engine
from app.main import create_app
from app.models import BackupSchedule, Base, Site, SiteDatabase, utcnow
from app.services import backup as backup_service


def _csrf(client) -> str:
    page = client.get("/backup")
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


@pytest.fixture
def test_site(db):
    """Create a temporary site for backup testing."""
    site = Site(
        name="backuptest",
        domain="backuptest.example.com",
        system_user="backuptest_user",
        root_dir="/tmp/lite-panel-test-site",
        webroot="/tmp/lite-panel-test-site/public",
    )
    db.add(site)
    db.commit()
    db.refresh(site)

    # Create dummy files
    p = Path(site.root_dir)
    p.mkdir(parents=True, exist_ok=True)
    (p / "index.html").write_text("<h1>Hello World</h1>")
    (p / "config.php").write_text("<?php echo 'secret';")

    return site


@pytest.fixture
def auth_client(db, admin):
    app = create_app()
    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 303
    return client


def test_create_backup_high_compression_files_and_db(test_site, monkeypatch, tmp_path):
    """Test creating a single high-compression archive with files and db."""
    monkeypatch.setattr(backup_service, "BACKUP_ROOT", tmp_path)

    # Mock databases.export_database to write dummy SQL
    from app.services import databases as db_service
    def mock_export(db_name, target_file, gzip=False):
        Path(target_file).write_text(f"CREATE TABLE `{db_name}` (id INT); INSERT INTO `{db_name}` VALUES (1);")

    monkeypatch.setattr(db_service, "export_database", mock_export)

    archive = backup_service.create_backup(
        site_name=test_site.name,
        root_dir=test_site.root_dir,
        db_names=["backuptest_db1", "backuptest_db2"],
        include_files=True,
        include_db=True,
    )

    assert archive.exists()
    assert archive.name.startswith("backuptest_full_")
    assert archive.name.endswith(".tar.gz")

    # Inspect the tar archive
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        assert "manifest.json" in names
        assert any(n.startswith("files") for n in names)
        assert "db/backuptest_db1.sql" in names
        assert "db/backuptest_db2.sql" in names

        # Check manifest content
        manifest_data = json.loads(tar.extractfile("manifest.json").read().decode("utf-8"))
        assert manifest_data["site_name"] == "backuptest"
        assert manifest_data["scope"] == "full"
        assert manifest_data["include_files"] is True
        assert manifest_data["include_db"] is True
        assert manifest_data["compression"] == "gzip-9"


def test_create_backup_files_only(test_site, monkeypatch, tmp_path):
    monkeypatch.setattr(backup_service, "BACKUP_ROOT", tmp_path)

    archive = backup_service.create_backup(
        site_name=test_site.name,
        root_dir=test_site.root_dir,
        db_names=[],
        include_files=True,
        include_db=False,
    )

    assert archive.name.startswith("backuptest_files_")
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        assert "manifest.json" in names
        assert any(n.startswith("files") for n in names)
        assert not any(n.startswith("db/") for n in names)


def test_create_backup_db_only(test_site, monkeypatch, tmp_path):
    monkeypatch.setattr(backup_service, "BACKUP_ROOT", tmp_path)

    from app.services import databases as db_service
    def mock_export(db_name, target_file, gzip=False):
        Path(target_file).write_text("CREATE DATABASE test;")
    monkeypatch.setattr(db_service, "export_database", mock_export)

    archive = backup_service.create_backup(
        site_name=test_site.name,
        root_dir=test_site.root_dir,
        db_names=["test_db"],
        include_files=False,
        include_db=True,
    )

    assert archive.name.startswith("backuptest_db_")
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        assert "manifest.json" in names
        assert not any(n.startswith("files") for n in names)
        assert "db/test_db.sql" in names


def test_create_backup_rejects_empty():
    with pytest.raises(ValueError, match="neither files nor database"):
        backup_service.create_backup(
            site_name="test",
            root_dir="/tmp",
            db_names=[],
            include_files=False,
            include_db=False,
        )


def test_schedule_descriptions_and_next_run(test_site):
    now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)

    # Daily schedule at 02:00 UTC
    sched_daily = BackupSchedule(
        site_id=test_site.id,
        frequency="daily",
        hour=2,
        minute=0,
    )
    assert backup_service.describe_schedule(sched_daily) == "Daily at 02:00 UTC"
    next_daily = backup_service.get_next_run(sched_daily, from_time=now)
    assert next_daily.day == 16
    assert next_daily.hour == 2

    # Twice daily at 02:00 and 14:00 UTC
    sched_twice = BackupSchedule(
        site_id=test_site.id,
        frequency="twice_daily",
        hour=2,
        minute=0,
    )
    assert backup_service.describe_schedule(sched_twice) == "Twice daily at 02:00 & 14:00 UTC"
    next_twice = backup_service.get_next_run(sched_twice, from_time=now)
    assert next_twice.day == 15
    assert next_twice.hour == 14

    # Weekly on Sunday (day_of_week=6) at 03:00 UTC
    sched_weekly = BackupSchedule(
        site_id=test_site.id,
        frequency="weekly",
        day_of_week=6,
        hour=3,
        minute=0,
    )
    assert "Weekly on Sunday at 03:00 UTC" in backup_service.describe_schedule(sched_weekly)


def test_check_schedule_due(test_site):
    sched = BackupSchedule(
        site_id=test_site.id,
        frequency="daily",
        hour=2,
        minute=0,
        is_enabled=True,
    )

    # Before 02:00 UTC on Sept 15: not due
    t1 = datetime(2026, 9, 15, 1, 30, 0, tzinfo=timezone.utc)
    assert backup_service.check_schedule_due(sched, now=t1) is False

    # At or after 02:00 UTC: due because last_run_at is None
    t2 = datetime(2026, 9, 15, 2, 5, 0, tzinfo=timezone.utc)
    assert backup_service.check_schedule_due(sched, now=t2) is True

    # If it just ran at 02:05: no longer due
    sched.last_run_at = t2
    assert backup_service.check_schedule_due(sched, now=t2) is False

    # If disabled: never due
    sched.is_enabled = False
    sched.last_run_at = None
    assert backup_service.check_schedule_due(sched, now=t2) is False


def test_web_backup_endpoints(auth_client, db, test_site):
    # 1. GET /backup
    res = auth_client.get("/backup")
    assert res.status_code == 200
    assert "Backups" in res.text
    assert test_site.domain in res.text

    token = _csrf(auth_client)

    # 2. POST /backup/schedule/create
    res_sched = auth_client.post(
        "/backup/schedule/create",
        data={
            "csrf_token": token,
            "site_id": test_site.id,
            "include_files": "true",
            "include_db": "true",
            "frequency": "daily",
            "hour": "3",
            "minute": "0",
        },
        follow_redirects=False,
    )
    assert res_sched.status_code == 303

    sched = db.query(BackupSchedule).filter_by(site_id=test_site.id).first()
    assert sched is not None
    assert sched.frequency == "daily"
    assert sched.hour == 3
    assert sched.is_enabled is True

    # 3. POST /backup/schedule/{id}/toggle
    token = _csrf(auth_client)
    res_toggle = auth_client.post(
        f"/backup/schedule/{sched.id}/toggle",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert res_toggle.status_code == 303
    db.refresh(sched)
    assert sched.is_enabled is False

    # 4. POST /backup/run (immediate run)
    token = _csrf(auth_client)
    res_run = auth_client.post(
        "/backup/run",
        data={
            "csrf_token": token,
            "site_id": test_site.id,
            "include_files": "true",
            "include_db": "true",
        },
        follow_redirects=False,
    )
    assert res_run.status_code == 303

    # 5. POST /backup/schedule/{id}/delete
    sched_id = sched.id
    token = _csrf(auth_client)
    res_del = auth_client.post(
        f"/backup/schedule/{sched_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert res_del.status_code == 303
    db.expire_all()
    assert db.get(BackupSchedule, sched_id) is None


def test_poll_and_run_schedules(db, test_site):
    from app.models import Job

    # Create an enabled schedule whose hour has already passed today
    now = datetime.now(timezone.utc)
    sched = BackupSchedule(
        site_id=test_site.id,
        frequency="daily",
        hour=max(0, now.hour - 1),
        minute=0,
        is_enabled=True,
    )
    db.add(sched)
    db.commit()

    count = backup_service.poll_and_run_schedules(db)
    assert count == 1

    db.refresh(sched)
    assert sched.last_run_at is not None

    # Verify a backup.create job was queued
    job = db.query(Job).filter_by(kind="backup.create").order_by(Job.id.desc()).first()
    assert job is not None
    payload = json.loads(job.payload)
    assert payload["site_id"] == test_site.id
    assert payload["schedule_id"] == sched.id


