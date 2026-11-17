"""Auto-deploy by polling the git host, the per-site deploy lock, and coalescing.

Polling is tested against *real* git (a bare repository plays GitHub) so
``ls-remote``, the "did the branch move" decision, rollback behaviour and
no-retry-loop are real; only the switch to the site user and the job worker
are stubbed.
"""

import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import jobs, tasks
from app.main import create_app
from app.models import Job, Site, SiteDeploy, SiteDeployment, utcnow
from app.services import deploy as deploy_service
from app.validators import ValidationError, validate_poll_minutes

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_poll_choices():
    assert validate_poll_minutes("") == 0 and validate_poll_minutes("0") == 0
    assert [validate_poll_minutes(m) for m in (1, 2, 5, 10, 15, 30, 60)] == [1, 2, 5, 10, 15, 30, 60]
    for bad in ("3", "-1", "x", "1440"):
        with pytest.raises(ValidationError):
            validate_poll_minutes(bad)


def test_the_recommended_interval_is_one_of_the_choices():
    from app.validators import POLL_MINUTES_CHOICES

    assert deploy_service.RECOMMENDED_POLL_MINUTES == 5
    assert deploy_service.RECOMMENDED_POLL_MINUTES in POLL_MINUTES_CHOICES


# --------------------------------------------------------------------------
# Fixtures: a git "GitHub", a site on disk, the DB rows
# --------------------------------------------------------------------------


@pytest.fixture
def origin(tmp_path):
    bare = tmp_path / "origin.git"
    work = tmp_path / "work"
    sh("git", "init", "-q", "--bare", "-b", "main", str(bare))
    sh("git", "clone", "-q", str(bare), str(work))
    for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
        sh("git", "config", key, value, cwd=work)
    sh("git", "checkout", "-q", "-b", "main", cwd=work)

    def commit(content, message):
        (work / "index.html").write_text(content)
        sh("git", "add", ".", cwd=work)
        sh("git", "commit", "-q", "-m", message, cwd=work)
        sh("git", "push", "-q", "origin", "main", cwd=work)
        return sh("git", "rev-parse", "HEAD", cwd=work)

    first = commit("v1", "first")
    return SimpleNamespace(url=str(bare), commit=commit, first=first)


@pytest.fixture(autouse=True)
def no_health_waits(monkeypatch):
    monkeypatch.setattr(deploy_service, "HEALTH_DELAY_SECONDS", 0)
    monkeypatch.setattr(deploy_service, "HEALTH_FIRST_WAIT_SECONDS", 0)


@pytest.fixture(autouse=True)
def real_git(monkeypatch):
    """Run git as the current user, and keep the job worker idle."""
    monkeypatch.setattr(
        deploy_service, "as_site_user",
        lambda site, args, extra_env=None: ["env", *deploy_service._git_env(site), *(extra_env or []), *args],
    )
    monkeypatch.setattr(deploy_service, "_after_deploy", lambda ctx, site: None)
    monkeypatch.setattr(jobs.worker, "submit", lambda job_id: None)


@pytest.fixture
def site(db, tmp_path, origin):
    root = tmp_path / "site"
    root.mkdir()
    row = Site(name="shop", domain="shop.example.com", system_user="site_shop",
               root_dir=str(root), webroot=str(root / "public"), php_version=None)
    db.add(row)
    db.flush()
    db.add(SiteDeployment(site_id=row.id, repo_url=origin.url, branch="main", repo_dir=str(root),
                          script="", webhook_token="tok", webhook_secret="sec", poll_minutes=5))
    db.commit()
    return row


class Ctx:
    def __init__(self, payload):
        self.payload = payload
        self.lines = []

    def log(self, line):
        self.lines.append(line)

    def run(self, args, **kw):
        from app.shell import stream

        kw.pop("label", None)
        return stream(args, self.lines.append, **kw)


def run_queued_deploy(db):
    """Execute the newest queued deploy job the way the worker would."""
    job = db.scalars(select(Job).where(Job.kind == "site.deploy").order_by(Job.id.desc())).first()
    payload = json.loads(job.payload)
    try:
        tasks.deploy_site(Ctx(payload))
    except jobs.JobFailed:
        pass
    db.expire_all()
    return job


def deploys(db):
    db.expire_all()
    return db.scalars(select(SiteDeploy).order_by(SiteDeploy.id)).all()


# --------------------------------------------------------------------------
# Is a check due?
# --------------------------------------------------------------------------


def test_due_logic(site, db):
    dep = site.deployment
    now = utcnow()
    assert deploy_service.poll_due(dep, now)                       # never checked
    dep.last_checked_at = now - timedelta(minutes=4)
    assert not deploy_service.poll_due(dep, now)
    dep.last_checked_at = now - timedelta(minutes=5)
    assert deploy_service.poll_due(dep, now)
    dep.poll_minutes = 0
    assert not deploy_service.poll_due(dep, now + timedelta(days=1))   # off


def test_remote_head_reads_the_branch_and_reports_errors(site, origin):
    assert deploy_service.remote_head(site, site.deployment) == origin.first
    site.deployment.branch = "nope"
    with pytest.raises(deploy_service.DeployError, match="not found"):
        deploy_service.remote_head(site, site.deployment)
    site.deployment.branch = "main"
    site.deployment.repo_url = "/does/not/exist.git"
    with pytest.raises(deploy_service.DeployError):
        deploy_service.remote_head(site, site.deployment)


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------


def test_a_site_that_was_never_deployed_is_not_deployed_by_the_first_check(site, db, origin):
    assert deploy_service.poll_deployment(db, site, site.deployment) == "current"
    assert deploys(db) == []
    assert site.deployment.last_remote_head == origin.first


def test_a_new_commit_queues_exactly_one_auto_deploy(site, db, origin):
    # First deploy by hand, so there is something checked out.
    deploy_service.queue_deploy(db, site, trigger="manual", username="admin")
    run_queued_deploy(db)
    assert site.deployment.last_remote_head == origin.first

    assert deploy_service.poll_deployment(db, site, site.deployment) == "current"   # nothing new
    assert len(deploys(db)) == 1

    second = origin.commit("v2", "second")
    assert deploy_service.poll_deployment(db, site, site.deployment) == "queued"
    new = deploys(db)[-1]
    assert (new.trigger, new.username, new.status) == ("auto", "auto-deploy", "queued")

    run_queued_deploy(db)
    assert deploys(db)[-1].status == "success" and deploys(db)[-1].commit == second
    assert (Path(site.root_dir) / "index.html").read_text() == "v2"

    # The next check sees the same head: no second deploy.
    assert deploy_service.poll_deployment(db, site, site.deployment) == "current"
    assert len(deploys(db)) == 2


def test_first_check_after_enabling_deploys_when_the_site_is_behind(site, db, origin):
    deploy_service.queue_deploy(db, site, trigger="manual", username="admin")
    run_queued_deploy(db)
    origin.commit("v2", "pushed while polling was off")
    site.deployment.last_remote_head = None                    # polling just switched on
    assert deploy_service.poll_deployment(db, site, site.deployment) == "queued"


def test_a_rollback_is_not_undone_by_polling(site, db, origin):
    deploy_service.queue_deploy(db, site, trigger="manual", username="admin")
    run_queued_deploy(db)
    origin.commit("v2", "second")
    deploy_service.queue_deploy(db, site, trigger="manual", username="admin")
    run_queued_deploy(db)
    head = site.deployment.last_remote_head

    # Roll back to the first commit (a pinned redeploy).
    deploy_service.queue_deploy(db, site, trigger="redeploy", commit=origin.first, username="admin")
    run_queued_deploy(db)
    assert (Path(site.root_dir) / "index.html").read_text() == "v1"
    assert site.deployment.last_remote_head == head            # unchanged by a pinned deploy

    count = len(deploys(db))
    assert deploy_service.poll_deployment(db, site, site.deployment) == "current"
    assert len(deploys(db)) == count                           # the rollback stays

    origin.commit("v3", "third")                               # a genuinely new push moves on
    assert deploy_service.poll_deployment(db, site, site.deployment) == "queued"


def test_a_failed_auto_deploy_is_not_retried_in_a_loop(site, db, origin):
    deploy_service.queue_deploy(db, site, trigger="manual", username="admin")
    run_queued_deploy(db)
    site.deployment.script = "false"                           # every deploy will fail now
    db.commit()
    origin.commit("v2", "breaks the build")

    assert deploy_service.poll_deployment(db, site, site.deployment) == "queued"
    run_queued_deploy(db)
    assert deploys(db)[-1].status == "failed"

    for _ in range(3):                                         # later checks stay quiet
        assert deploy_service.poll_deployment(db, site, site.deployment) == "current"
    assert len(deploys(db)) == 2


def test_a_failed_check_is_recorded_and_clears_when_it_works_again(site, db, origin):
    site.deployment.repo_url = "/does/not/exist.git"
    result = deploy_service.poll_deployment(db, site, site.deployment)
    assert result.startswith("error")
    assert site.deployment.last_check_error and site.deployment.last_checked_at

    site.deployment.repo_url = origin.url
    deploy_service.poll_deployment(db, site, site.deployment)
    assert site.deployment.last_check_error is None


# --------------------------------------------------------------------------
# poll_once: only due deployments, one failure never stops the rest
# --------------------------------------------------------------------------


def test_poll_once_checks_only_due_deployments(site, db, monkeypatch):
    seen = []
    monkeypatch.setattr(deploy_service, "poll_deployment", lambda db_, s, d, now=None: seen.append(s.name) or "current")
    assert deploy_service.poll_once() == 1 and seen == ["shop"]

    db.expire_all()
    dep = db.scalars(select(SiteDeployment)).first()
    dep.last_checked_at = utcnow()                             # just checked
    db.commit()
    assert deploy_service.poll_once() == 0

    dep.poll_minutes = 0                                       # off
    dep.last_checked_at = None
    db.commit()
    assert deploy_service.poll_once() == 0


def test_one_broken_site_does_not_stop_the_pass(site, db, tmp_path, monkeypatch):
    other = Site(name="blog", domain="blog.example.com", system_user="site_blog",
                 root_dir=str(tmp_path / "blog"), webroot=str(tmp_path / "blog"))
    db.add(other)
    db.flush()
    db.add(SiteDeployment(site_id=other.id, repo_url="/x.git", branch="main", repo_dir=str(tmp_path / "blog"),
                          webhook_token="t2", webhook_secret="s2", poll_minutes=5))
    db.commit()
    seen = []

    def fake(db_, s, d, now=None):
        seen.append(s.name)
        if s.name == "shop":
            raise RuntimeError("boom")
        return "current"

    monkeypatch.setattr(deploy_service, "poll_deployment", fake)
    deploy_service.poll_once()
    assert sorted(seen) == ["blog", "shop"]


def test_the_poller_thread_starts_and_stops_cleanly():
    deploy_service.start_poller()
    assert deploy_service._poller_thread.is_alive()
    deploy_service.stop_poller()
    assert not deploy_service._poller_thread.is_alive()


# --------------------------------------------------------------------------
# One deploy at a time, and coalescing
# --------------------------------------------------------------------------


def test_a_person_cannot_start_a_second_deploy_while_one_is_running(site, db):
    deploy_service.queue_deploy(db, site, trigger="manual", username="admin")
    with pytest.raises(deploy_service.DeployError, match="already running"):
        deploy_service.queue_deploy(db, site, trigger="manual", username="admin")
    assert len(deploys(db)) == 1


def test_automatic_triggers_during_a_deploy_collapse_into_one_rerun(site, db, origin):
    deploy_service.queue_deploy(db, site, trigger="manual", username="admin")    # running
    for trigger in ("webhook", "auto", "webhook"):
        assert deploy_service.queue_deploy(db, site, trigger=trigger) is None
    assert len(deploys(db)) == 1 and site.deployment.rerun_pending

    origin.commit("v2", "pushed during the deploy")
    run_queued_deploy(db)                                      # first deploy finishes...
    db.expire_all()
    rows = deploys(db)
    assert len(rows) == 2 and rows[1].trigger == "auto" and rows[1].status == "queued"   # ...one rerun queued
    assert not db.scalars(select(SiteDeployment)).first().rerun_pending

    run_queued_deploy(db)
    assert deploys(db)[-1].status == "success"
    assert (Path(site.root_dir) / "index.html").read_text() == "v2"             # the newest commit


def test_the_rerun_is_queued_even_when_the_first_deploy_fails(site, db):
    site.deployment.script = "false"
    db.commit()
    deploy_service.queue_deploy(db, site, trigger="manual", username="admin")
    deploy_service.queue_deploy(db, site, trigger="webhook")
    run_queued_deploy(db)
    rows = deploys(db)
    assert [r.status for r in rows] == ["failed", "queued"]


def test_a_stale_running_row_does_not_lock_the_site_forever(site, db):
    old = SiteDeploy(site_id=site.id, trigger="manual", status="running",
                     started_at=utcnow() - timedelta(hours=3))
    db.add(old)
    db.commit()
    assert deploy_service.active_deploy(db, site.id) is None
    assert deploy_service.queue_deploy(db, site, trigger="manual", username="admin") is not None


def test_deploys_interrupted_by_a_restart_are_marked_failed(site, db):
    db.add(SiteDeploy(site_id=site.id, trigger="manual", status="running"))
    db.add(SiteDeploy(site_id=site.id, trigger="webhook", status="queued"))
    db.add(SiteDeploy(site_id=site.id, trigger="manual", status="success"))
    db.commit()
    assert deploy_service.mark_interrupted(db) == 2
    db.commit()
    assert sorted(d.status for d in deploys(db)) == ["failed", "failed", "success"]
    assert deploy_service.active_deploy(db, site.id) is None


# --------------------------------------------------------------------------
# Web
# --------------------------------------------------------------------------


@pytest.fixture
def web(monkeypatch, db, admin, site):
    fake = MagicMock()
    fake.is_installed.return_value = True
    fake.installed_versions.return_value = []
    monkeypatch.setattr("app.routers.sites.get_provider", lambda key: fake)
    with TestClient(create_app(), follow_redirects=False) as client:
        assert client.post("/login", data={"username": "admin", "password": "correct-horse-battery"}).status_code == 303
        yield client


def _csrf(client, path):
    page = client.get(path)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


def test_the_interval_picker_lists_the_choices_and_marks_the_recommended_one(web, site):
    page = web.get(f"/sites/{site.id}").text
    assert 'name="poll_minutes"' in page and "Check git for new commits" in page
    assert "Every minute" in page and "Every 5 minutes (recommended)" in page and "Every 60 minutes" in page
    assert 'value="5" selected' in page                        # the fixture polls every 5


def test_saving_the_interval_resets_the_check_state_and_returns_with_details_open(web, db, site):
    dep = site.deployment
    dep.last_remote_head, dep.last_check_error = "abc", "old error"
    db.commit()
    response = web.post(f"/sites/{site.id}/deploy/auto", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), "poll_minutes": "15",
    })
    assert response.status_code == 303 and response.headers["location"].endswith("#deploy-details")
    db.expire_all()
    dep = db.scalars(select(SiteDeployment)).first()
    assert (dep.poll_minutes, dep.last_remote_head, dep.last_check_error, dep.last_checked_at) == (15, None, None, None)


def test_an_invalid_interval_is_rejected_and_off_is_accepted(web, db, site):
    token = _csrf(web, f"/sites/{site.id}")
    bad = web.post(f"/sites/{site.id}/deploy/auto", data={"csrf_token": token, "poll_minutes": "7"})
    assert "error=" in bad.headers["location"]
    web.post(f"/sites/{site.id}/deploy/auto", data={"csrf_token": token, "poll_minutes": "0"})
    db.expire_all()
    assert db.scalars(select(SiteDeployment)).first().poll_minutes == 0


def test_the_page_shows_the_last_check_and_any_error(web, db, site):
    dep = site.deployment
    dep.last_checked_at = utcnow()
    db.commit()
    assert "no problems" in web.get(f"/sites/{site.id}").text
    dep.last_check_error = "Permission denied (publickey)."
    db.commit()
    page = web.get(f"/sites/{site.id}").text
    assert "Last check failed: Permission denied (publickey)." in page and "deploy key added" in page


def test_deploy_now_while_one_is_running_shows_an_error_not_a_second_job(web, db, site):
    token = _csrf(web, f"/sites/{site.id}")
    web.post(f"/sites/{site.id}/deploy", data={"csrf_token": token})
    response = web.post(f"/sites/{site.id}/deploy", data={"csrf_token": token})
    assert "already%20running" in response.headers["location"]
    assert len(deploys(db)) == 1


def test_a_webhook_during_a_deploy_is_accepted_and_deferred(db, site, monkeypatch):
    import hashlib
    import hmac

    dep = site.deployment
    dep.webhook_enabled = True
    db.commit()
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    signature = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
    with TestClient(create_app(), follow_redirects=False) as client:
        # Queued after the app started: a restart would (correctly) mark it interrupted.
        deploy_service.queue_deploy(db, site, trigger="manual", username="admin")
        response = client.post("/hooks/deploy/tok", content=body, headers={
            "Content-Type": "application/json", "X-GitHub-Event": "push", "X-Hub-Signature-256": signature,
        })
    assert response.status_code == 202 and "after the running deploy" in response.json()["queued"]
    db.expire_all()
    assert db.scalars(select(SiteDeployment)).first().rerun_pending
    assert len(deploys(db)) == 1


def test_starting_the_app_marks_deploys_from_the_previous_process_as_failed(db, site):
    db.add(SiteDeploy(site_id=site.id, trigger="manual", status="running"))
    db.commit()
    with TestClient(create_app(), follow_redirects=False):
        pass
    assert [d.status for d in deploys(db)] == ["failed"]
    assert "restarted" in deploys(db)[0].error


def test_existing_databases_get_the_new_columns(tmp_path):
    """An install from before polling has site_deployments without them."""
    import sqlite3

    from sqlalchemy import create_engine

    from app import database
    from app.models import Base

    path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:                      # recreate the pre-polling shape
        for column in ("poll_minutes", "last_checked_at", "last_check_error", "last_remote_head", "rerun_pending"):
            conn.exec_driver_sql(f"ALTER TABLE site_deployments DROP COLUMN {column}")
    original = database.engine
    database.engine = engine
    try:
        database._ensure_additive_columns()
    finally:
        database.engine = original
    con = sqlite3.connect(path)
    columns = {row[1] for row in con.execute("PRAGMA table_info(site_deployments)")}
    con.close()
    assert {"poll_minutes", "last_checked_at", "last_check_error", "last_remote_head", "rerun_pending"} <= columns
