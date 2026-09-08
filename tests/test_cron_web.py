"""HTTP-level tests for the cron router.

The job worker is never started here -- these only need to confirm that a
route validates input, writes the right CronJob row, and enqueues a
"cron.sync" job with the right payload. Actually installing a crontab
requires a real system user and the ``crontab`` binary, which is exactly
what app.services.cron's own tests (test_cron_service.py) cover in
isolation.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import jobs
from app.main import create_app
from app.models import CronJob, Job, Site


@pytest.fixture(autouse=True)
def isolated_worker_queue(monkeypatch):
    """``enqueue()`` hands every job straight to the module-level
    ``jobs.worker`` singleton, which is shared with test_jobs.py's tests in
    the same process. Since nothing here calls ``worker.start()``, letting
    real job ids pile up in its queue would surface later as spurious
    failures once that other file's tests start it -- so submission is a
    no-op here. The Job row itself is still committed by enqueue() before it
    calls submit(), so assertions against the database are unaffected.
    """
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


@pytest.fixture
def site(db):
    site = Site(
        name="demo",
        domain="demo.example.com",
        system_user="site_demo",
        root_dir="/var/www/demo",
        webroot="/var/www/demo",
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def _csrf(client) -> str:
    page = client.get("/cron")
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


def _last_job(db) -> Job:
    return db.scalars(select(Job).order_by(Job.id.desc())).first()


# --------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------


def test_cron_page_requires_login(client):
    assert client.get("/cron").status_code == 303


def test_create_requires_csrf(signed_in, site):
    response = signed_in.post(
        "/cron/new",
        data={
            "site_id": site.id,
            "minute": "*",
            "hour": "*",
            "day_of_month": "*",
            "month": "*",
            "day_of_week": "*",
            "command": "true",
        },
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------


def test_create_saves_the_job_and_enqueues_a_sync(signed_in, db, site):
    token = _csrf(signed_in)

    response = signed_in.post(
        "/cron/new",
        data={
            "site_id": site.id,
            "description": "Nightly backup",
            "minute": "0",
            "hour": "3",
            "day_of_month": "*",
            "month": "*",
            "day_of_week": "*",
            "command": "/usr/bin/backup.sh",
            "csrf_token": token,
        },
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")

    job = db.scalars(select(CronJob)).one()
    assert job.site_id == site.id
    assert job.description == "Nightly backup"
    assert job.command == "/usr/bin/backup.sh"
    assert job.is_enabled is True

    sync_job = _last_job(db)
    assert sync_job.kind == "cron.sync"
    assert json.loads(sync_job.payload) == {"site_id": site.id}


def test_create_rejects_a_malformed_schedule_field(signed_in, db, site):
    token = _csrf(signed_in)

    response = signed_in.post(
        "/cron/new",
        data={
            "site_id": site.id,
            "minute": "* ; rm -rf /",
            "hour": "*",
            "day_of_month": "*",
            "month": "*",
            "day_of_week": "*",
            "command": "true",
            "csrf_token": token,
        },
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert db.scalars(select(CronJob)).all() == []


def test_create_against_a_missing_site_fails_cleanly(signed_in, db):
    token = _csrf(signed_in)
    response = signed_in.post(
        "/cron/new",
        data={
            "site_id": 999,
            "minute": "*",
            "hour": "*",
            "day_of_month": "*",
            "month": "*",
            "day_of_week": "*",
            "command": "true",
            "csrf_token": token,
        },
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_create_rejects_a_garbage_site_id(signed_in, db):
    token = _csrf(signed_in)
    response = signed_in.post(
        "/cron/new",
        data={
            "site_id": "not-a-number-or-server",
            "minute": "*",
            "hour": "*",
            "day_of_month": "*",
            "month": "*",
            "day_of_week": "*",
            "command": "true",
            "csrf_token": token,
        },
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert db.scalars(select(CronJob)).all() == []


# --------------------------------------------------------------------------
# Server-wide (root) scope
# --------------------------------------------------------------------------


def test_cron_page_offers_the_form_with_no_sites_at_all(signed_in):
    """The whole point: a box with no sites yet still has somewhere to
    schedule a cron job, instead of being told to go create a site first."""
    response = signed_in.get("/cron")
    assert response.status_code == 200
    assert "Server (root)" in response.text
    assert 'name="site_id"' in response.text


def test_create_with_server_scope_creates_a_site_less_job(signed_in, db):
    token = _csrf(signed_in)

    response = signed_in.post(
        "/cron/new",
        data={
            "site_id": "server",
            "description": "Full server backup",
            "minute": "0",
            "hour": "4",
            "day_of_month": "*",
            "month": "*",
            "day_of_week": "*",
            "command": "/usr/local/bin/backup-everything.sh",
            "csrf_token": token,
        },
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")

    job = db.scalars(select(CronJob)).one()
    assert job.site_id is None
    assert job.target_label == "Server (root)"
    assert job.target_user == "root"

    sync_job = _last_job(db)
    assert sync_job.kind == "cron.sync"
    assert json.loads(sync_job.payload) == {"site_id": None}


def test_cron_page_lists_a_server_job_with_a_root_badge(signed_in, db):
    server_job = CronJob(
        site_id=None,
        minute="*",
        hour="*",
        day_of_month="*",
        month="*",
        day_of_week="*",
        command="echo server",
    )
    db.add(server_job)
    db.commit()

    response = signed_in.get("/cron")
    assert "Server (root)" in response.text
    assert "echo server" in response.text


def test_toggle_and_delete_a_server_job(signed_in, db):
    server_job = CronJob(
        site_id=None,
        minute="*",
        hour="*",
        day_of_month="*",
        month="*",
        day_of_week="*",
        command="echo server",
    )
    db.add(server_job)
    db.commit()
    db.refresh(server_job)
    token = _csrf(signed_in)

    toggle_response = signed_in.post(
        f"/cron/{server_job.id}/toggle", data={"csrf_token": token}
    )
    assert toggle_response.status_code == 303
    db.refresh(server_job)
    assert server_job.is_enabled is False
    assert json.loads(_last_job(db).payload) == {"site_id": None}

    delete_response = signed_in.post(
        f"/cron/{server_job.id}/delete", data={"csrf_token": token}
    )
    assert delete_response.status_code == 303
    db.expire_all()
    assert db.scalars(select(CronJob)).all() == []
    assert json.loads(_last_job(db).payload) == {"site_id": None}


# --------------------------------------------------------------------------
# Edit / toggle / delete
# --------------------------------------------------------------------------


@pytest.fixture
def job(db, site):
    job = CronJob(
        site_id=site.id,
        minute="*",
        hour="*",
        day_of_month="*",
        month="*",
        day_of_week="*",
        command="echo hi",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_edit_page_renders(signed_in, job):
    response = signed_in.get(f"/cron/{job.id}/edit")
    assert response.status_code == 200
    assert "echo hi" in response.text


def test_edit_page_selects_the_matching_preset_and_hides_the_raw_fields(signed_in, job):
    """job's fixture schedule is "* * * * *" -- an exact match for the
    "Every minute" preset, so it should come up pre-selected with the raw
    field inputs tucked away, not defaulted to "Custom"."""
    response = signed_in.get(f"/cron/{job.id}/edit")
    assert 'value="* * * * *" selected' in response.text
    assert 'value="custom" selected' not in response.text
    assert "data-cron-custom hidden" in response.text


def test_edit_page_falls_back_to_custom_for_an_unusual_schedule(signed_in, db, site):
    odd_job = CronJob(
        site_id=site.id,
        minute="7",
        hour="3",
        day_of_month="1",
        month="6",
        day_of_week="2",
        command="echo odd",
    )
    db.add(odd_job)
    db.commit()
    db.refresh(odd_job)

    response = signed_in.get(f"/cron/{odd_job.id}/edit")
    assert 'value="custom" selected' in response.text
    assert "data-cron-custom hidden" not in response.text


def test_cron_page_lists_the_schedule_presets(signed_in, site):
    response = signed_in.get("/cron")
    assert "Every minute" in response.text
    assert "Every 5 minutes" in response.text
    assert "Hourly" in response.text
    assert "Custom…" in response.text


def test_cron_page_shows_the_php_cli_path_when_php_is_installed(signed_in, site, monkeypatch):
    import app.routers.cron as cron_router

    monkeypatch.setattr(cron_router, "_php_cli_paths", lambda: ["/usr/bin/php8.3"])
    response = signed_in.get("/cron")
    assert "/usr/bin/php8.3" in response.text


def test_edit_updates_the_row_and_enqueues_a_sync(signed_in, db, job):
    token = _csrf(signed_in)
    response = signed_in.post(
        f"/cron/{job.id}/edit",
        data={
            "description": "updated",
            "minute": "*/5",
            "hour": "*",
            "day_of_month": "*",
            "month": "*",
            "day_of_week": "*",
            "command": "echo bye",
            "csrf_token": token,
        },
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")

    db.refresh(job)
    assert job.minute == "*/5"
    assert job.command == "echo bye"
    assert _last_job(db).kind == "cron.sync"


def test_edit_with_bad_input_redirects_back_to_the_edit_form(signed_in, db, job):
    token = _csrf(signed_in)
    original_command = job.command

    response = signed_in.post(
        f"/cron/{job.id}/edit",
        data={
            "minute": "*",
            "hour": "*",
            "day_of_month": "*",
            "month": "notanumber",
            "day_of_week": "*",
            "command": "echo bye",
            "csrf_token": token,
        },
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/cron/{job.id}/edit?error=")
    assert "Month" in location

    db.refresh(job)
    assert job.command == original_command


def test_toggle_flips_enabled_state_and_enqueues_a_sync(signed_in, db, job):
    token = _csrf(signed_in)
    assert job.is_enabled is True

    response = signed_in.post(f"/cron/{job.id}/toggle", data={"csrf_token": token})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")

    db.refresh(job)
    assert job.is_enabled is False
    assert _last_job(db).kind == "cron.sync"


def test_delete_removes_the_row_and_enqueues_a_sync(signed_in, db, job):
    token = _csrf(signed_in)
    job_id = job.id

    response = signed_in.post(f"/cron/{job_id}/delete", data={"csrf_token": token})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")

    db.expire_all()
    assert db.scalars(select(CronJob)).all() == []
    assert _last_job(db).kind == "cron.sync"


def test_acting_on_a_missing_job_fails_cleanly(signed_in):
    token = _csrf(signed_in)
    response = signed_in.post("/cron/999/toggle", data={"csrf_token": token})
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
