"""Health check after a deploy, automatic rollback, and failure alerts.

Rollback is tested with *real* git: a bare repository plays GitHub, the site
folder is a real checkout, and "the site is broken" is simulated by what the
health probe answers for the checked-out commit.  Chat delivery is stubbed at
the HTTP layer, and the SSRF guard is tested on its own.
"""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import jobs, tasks
from app.main import create_app
from app.models import Job, Site, SiteDeploy, SiteDeployment
from app.services import deploy as deploy_service
from app.services import notify as notify_service
from app.services import permissions as permissions_service
from app.validators import ValidationError, validate_health_path, validate_notify_url

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_health_path():
    assert validate_health_path("") == "/" and validate_health_path("/up") == "/up"
    assert validate_health_path("/api/health?full=1") == "/api/health?full=1"
    for bad in ("up", "/a b", "/x\r\nHost: evil", "/" + "a" * 250):
        with pytest.raises(ValidationError):
            validate_health_path(bad)


def test_alert_url_must_be_public_https():
    assert validate_notify_url("") == ""
    assert validate_notify_url("https://hooks.slack.com/services/T/B/x") == "https://hooks.slack.com/services/T/B/x"
    for bad in ("http://hooks.slack.com/x", "ftp://x/y", "hooks.slack.com/x", "https://a b/c", "https://x/\nHost: y"):
        with pytest.raises(ValidationError):
            validate_notify_url(bad)


# --------------------------------------------------------------------------
# The alert sender
# --------------------------------------------------------------------------


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(notify_service.socket, "getaddrinfo", lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", 443))])


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.9", "169.254.169.254", "::1"])
def test_private_addresses_are_refused(monkeypatch, address):
    monkeypatch.setattr(notify_service.socket, "getaddrinfo", lambda host, port, **kw: [(2, 1, 6, "", (address, 443))])
    with pytest.raises(notify_service.NotifyError, match="private address"):
        notify_service.send("https://hook.example.com/x", "hi")


def test_only_https_is_accepted():
    with pytest.raises(notify_service.NotifyError, match="https"):
        notify_service.send("http://hooks.example.com/x", "hi")


def test_the_body_works_for_slack_and_discord(public_dns, monkeypatch):
    sent = {}

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeOpener:
        def open(self, request, timeout):
            sent["url"], sent["body"], sent["timeout"] = request.full_url, json.loads(request.data), timeout
            return Response()

    monkeypatch.setattr(notify_service.urllib.request, "build_opener", lambda *handlers: FakeOpener())
    notify_service.send("https://hook.example.com/x", "boom", event="deploy_failed", site="shop.example.com")
    assert sent["url"] == "https://hook.example.com/x" and sent["timeout"] == notify_service.TIMEOUT_SECONDS
    assert sent["body"]["text"] == "boom" and sent["body"]["content"] == "boom"      # Slack and Discord
    assert sent["body"]["event"] == "deploy_failed" and sent["body"]["site"] == "shop.example.com"


def test_redirects_are_never_followed():
    handler = notify_service._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/") is None


def test_http_errors_and_unreachable_hosts_become_readable_errors(public_dns, monkeypatch):
    import urllib.error

    class Opener:
        def __init__(self, error):
            self.error = error

        def open(self, request, timeout):
            raise self.error

    monkeypatch.setattr(notify_service.urllib.request, "build_opener",
                        lambda *h: Opener(urllib.error.HTTPError("u", 404, "nf", {}, None)))
    with pytest.raises(notify_service.NotifyError, match="HTTP 404"):
        notify_service.send("https://hook.example.com/x", "hi")
    monkeypatch.setattr(notify_service.urllib.request, "build_opener",
                        lambda *h: Opener(urllib.error.URLError("timed out")))
    with pytest.raises(notify_service.NotifyError, match="Could not reach"):
        notify_service.send("https://hook.example.com/x", "hi")


# --------------------------------------------------------------------------
# Fixtures for real deploys
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fast_and_local(monkeypatch):
    monkeypatch.setattr(deploy_service, "HEALTH_DELAY_SECONDS", 0)
    monkeypatch.setattr(deploy_service, "HEALTH_FIRST_WAIT_SECONDS", 0)
    monkeypatch.setattr(
        deploy_service, "as_site_user",
        lambda site, args, extra_env=None: ["env", *deploy_service._git_env(site), *(extra_env or []), *args],
    )
    monkeypatch.setattr(deploy_service, "_after_deploy", lambda ctx, site: None)
    monkeypatch.setattr(jobs.worker, "submit", lambda job_id: None)


@pytest.fixture
def origin(tmp_path):
    bare, work = tmp_path / "origin.git", tmp_path / "work"
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

    return SimpleNamespace(url=str(bare), commit=commit)


@pytest.fixture
def site(db, tmp_path, origin):
    root = tmp_path / "site"
    root.mkdir()
    row = Site(name="shop", domain="shop.example.com", system_user="site_shop",
               root_dir=str(root), webroot=str(root / "public"), php_version=None)
    db.add(row)
    db.flush()
    db.add(SiteDeployment(site_id=row.id, repo_url=origin.url, branch="main", repo_dir=str(root),
                          script="", webhook_token="tok", webhook_secret="sec",
                          notify_url="https://hook.example.com/x"))
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


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(notify_service, "send", lambda url, text, **kw: sent.append((url, text, kw.get("event"))))
    return sent


@pytest.fixture
def probe(monkeypatch, site):
    """What nginx answers: broken whenever the checked-out page says BROKEN."""
    def status(domain, https, path="/"):
        page = Path(site.root_dir) / "index.html"
        return 500 if page.exists() and "BROKEN" in page.read_text() else 200

    monkeypatch.setattr(permissions_service, "http_status", status)
    return status


def deploy(db, site, *, trigger="manual", commit=None):
    """Queue and run one deploy the way the worker does; return (job outcome, rows)."""
    job = deploy_service.queue_deploy(db, site, trigger=trigger, commit=commit, username="admin")
    ctx = Ctx(json.loads(job.payload))
    error = None
    try:
        tasks.deploy_site(ctx)
    except jobs.JobFailed as exc:
        error = str(exc)
    db.expire_all()
    return error, ctx, db.scalars(select(SiteDeploy).order_by(SiteDeploy.id)).all()


def settings(db, **values):
    dep = db.scalars(select(SiteDeployment)).first()
    for key, value in values.items():
        setattr(dep, key, value)
    db.commit()
    return dep


# --------------------------------------------------------------------------
# Health check
# --------------------------------------------------------------------------


def test_a_healthy_deploy_records_its_health_and_sends_no_alert(db, site, origin, probe, alerts):
    origin.commit("v1", "first")
    error, ctx, rows = deploy(db, site)
    assert error is None
    assert (rows[0].status, rows[0].health) == ("success", "HTTP 200")
    assert alerts == []                                                 # failures only by default
    assert any("Health check: / answered HTTP 200" in line for line in ctx.lines)


def test_success_alerts_are_opt_in(db, site, origin, probe, alerts):
    origin.commit("v1", "first")
    settings(db, notify_success=True)
    deploy(db, site)
    assert [event for _, _, event in alerts] == ["deploy_succeeded"] and "deployed" in alerts[0][1]


def test_redirects_and_404_are_not_failures_but_403_and_5xx_are(db, site, origin, monkeypatch):
    ctx = Ctx({})
    dep = SimpleNamespace(health_path="/")
    for status, ok in ((200, True), (302, True), (404, True), (403, False), (500, False), (503, False)):
        monkeypatch.setattr(permissions_service, "http_status", lambda d, https, path="/", s=status: s)
        assert deploy_service.check_health(ctx, SimpleNamespace(domain="x", ssl_enabled=False), dep).ok is ok


def test_the_site_gets_a_few_seconds_to_come_back(monkeypatch):
    answers = iter([502, 502, 200])
    monkeypatch.setattr(permissions_service, "http_status", lambda d, https, path="/": next(answers))
    result = deploy_service.check_health(Ctx({}), SimpleNamespace(domain="x", ssl_enabled=False), SimpleNamespace(health_path="/up"))
    assert result.ok and result.text == "HTTP 200"


def test_no_answer_from_nginx_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(permissions_service, "http_status", lambda d, https, path="/": None)
    ctx = Ctx({})
    result = deploy_service.check_health(ctx, SimpleNamespace(domain="x", ssl_enabled=False), SimpleNamespace(health_path="/"))
    assert result.ok and result.text == "not verified" and any("could not be verified" in l for l in ctx.lines)


def test_the_configured_path_is_requested(monkeypatch):
    seen = []
    monkeypatch.setattr(permissions_service, "http_status", lambda d, https, path="/": seen.append(path) or 200)
    deploy_service.check_health(Ctx({}), SimpleNamespace(domain="x", ssl_enabled=True), SimpleNamespace(health_path="/up"))
    assert seen == ["/up"]


# --------------------------------------------------------------------------
# A broken deploy
# --------------------------------------------------------------------------


def test_a_broken_deploy_without_rollback_is_marked_unhealthy_and_alerted(db, site, origin, probe, alerts):
    origin.commit("v1", "good")
    deploy(db, site)
    origin.commit("BROKEN", "bad change")
    error, _, rows = deploy(db, site)

    assert "answers HTTP 500" in error
    assert rows[-1].status == "unhealthy" and rows[-1].health == "HTTP 500"
    assert (Path(site.root_dir) / "index.html").read_text() == "BROKEN"       # left as is: rollback is off
    assert [event for _, _, event in alerts] == ["deploy_unhealthy"] and "rollback is off" in alerts[0][1]


def test_rollback_goes_back_to_the_last_working_commit(db, site, origin, probe, alerts):
    good = origin.commit("v1", "good")
    deploy(db, site)
    settings(db, rollback_on_failure=True)
    bad = origin.commit("BROKEN", "bad change")

    error, ctx, rows = deploy(db, site)

    assert (Path(site.root_dir) / "index.html").read_text() == "v1"           # back on the good code
    first, broken, rollback = rows
    assert broken.status == "rolled_back" and broken.commit == bad
    assert f"rolled back to {good[:7]}" in broken.error
    assert (rollback.trigger, rollback.status, rollback.commit, rollback.username) == (
        "rollback", "success", good, "auto-rollback")
    assert "rolled back" in error                                              # the job is shown as failed
    assert [event for _, _, event in alerts] == ["rolled_back"] and "migrations are not undone" in alerts[0][1]
    assert any("Rolling back to" in line for line in ctx.lines)


def test_the_bad_commit_is_not_deployed_again_by_polling(db, site, origin, probe, alerts):
    origin.commit("v1", "good")
    deploy(db, site)
    settings(db, rollback_on_failure=True, poll_minutes=5)
    bad = origin.commit("BROKEN", "bad change")
    deploy(db, site)
    dep = db.scalars(select(SiteDeployment)).first()
    assert dep.last_remote_head == bad                       # remembered as seen: no retry loop
    count = len(db.scalars(select(SiteDeploy)).all())
    assert deploy_service.poll_deployment(db, site, dep) == "current"
    assert len(db.scalars(select(SiteDeploy)).all()) == count


def test_the_rollback_target_skips_unhealthy_and_failed_deploys(db, site, origin, probe, alerts):
    good = origin.commit("v1", "good")
    deploy(db, site)
    settings(db, rollback_on_failure=False)
    origin.commit("BROKEN", "bad 1")
    deploy(db, site)                                           # unhealthy, stays deployed
    settings(db, rollback_on_failure=True)
    origin.commit("BROKEN again", "bad 2")
    deploy(db, site)
    assert (Path(site.root_dir) / "index.html").read_text() == "v1"           # back to the last *good* one
    assert db.scalars(select(SiteDeploy).where(SiteDeploy.trigger == "rollback")).first().commit == good


def test_nothing_to_roll_back_to_is_reported(db, site, origin, probe, alerts):
    settings(db, rollback_on_failure=True)
    origin.commit("BROKEN", "very first deploy is broken")
    error, _, rows = deploy(db, site)
    assert "no earlier successful deploy" in error
    assert rows[0].status == "unhealthy" and [e for _, _, e in alerts] == ["deploy_unhealthy"]


def test_a_rollback_that_is_also_broken_is_flagged_as_urgent(db, site, origin, monkeypatch, alerts):
    origin.commit("v1", "good")
    monkeypatch.setattr(permissions_service, "http_status", lambda d, https, path="/": 200)
    deploy(db, site)
    settings(db, rollback_on_failure=True)
    origin.commit("v2", "bad")
    monkeypatch.setattr(permissions_service, "http_status", lambda d, https, path="/": 500)   # everything is down
    error, _, rows = deploy(db, site)
    assert "still HTTP 500 after rolling back" in error
    assert rows[1].status == "unhealthy" and rows[2].status == "unhealthy"
    assert [e for _, _, e in alerts] == ["rollback_failed"] and "Act now" in alerts[0][1]


def test_a_rollback_that_fails_to_run_is_flagged(db, site, origin, probe, alerts, monkeypatch):
    origin.commit("v1", "good")
    deploy(db, site)
    settings(db, rollback_on_failure=True)
    origin.commit("BROKEN", "bad")
    real = deploy_service.deploy

    def flaky(ctx, s, d, commit=None):
        if commit:                                              # only the rollback fails
            raise deploy_service.DeployError("git checkout failed")
        return real(ctx, s, d, commit=commit)

    monkeypatch.setattr(deploy_service, "deploy", flaky)
    error, _, rows = deploy(db, site)
    assert "the rollback failed too" in error and rows[-1].status == "failed"
    assert [e for _, _, e in alerts] == ["rollback_failed"]


# --------------------------------------------------------------------------
# Failure alerts
# --------------------------------------------------------------------------


def test_a_failing_script_sends_an_alert(db, site, origin, probe, alerts):
    origin.commit("v1", "first")
    settings(db, script="exit 3")
    error, _, rows = deploy(db, site)
    assert rows[0].status == "failed" and "exit code 3" in error
    assert [e for _, _, e in alerts] == ["deploy_failed"] and "exit code 3" in alerts[0][1]


def test_no_webhook_means_no_alert_and_no_error(db, site, origin, probe, alerts):
    origin.commit("v1", "first")
    settings(db, script="false", notify_url=None)
    deploy(db, site)
    assert alerts == []


def test_a_broken_alert_webhook_never_breaks_the_deploy(db, site, origin, probe, monkeypatch):
    origin.commit("v1", "first")

    def down(url, text, **kw):
        raise notify_service.NotifyError("Could not reach the webhook: timed out")

    monkeypatch.setattr(notify_service, "send", down)
    settings(db, notify_success=True)
    error, ctx, rows = deploy(db, site)
    assert error is None and rows[0].status == "success"
    assert any("Could not send the alert" in line for line in ctx.lines)


def test_a_failing_repository_check_alerts_once_per_streak(db, site, origin, alerts):
    settings(db, poll_minutes=5, repo_url="/does/not/exist.git")
    dep = db.scalars(select(SiteDeployment)).first()
    for _ in range(3):
        assert deploy_service.poll_deployment(db, site, dep).startswith("error")
    assert [e for _, _, e in alerts] == ["check_failed"]                       # not three alerts

    dep.repo_url = origin.url
    origin.commit("v1", "first")
    deploy_service.poll_deployment(db, site, dep)                              # recovers...
    dep.repo_url = "/does/not/exist.git"
    deploy_service.poll_deployment(db, site, dep)                              # ...fails again: new streak
    assert [e for _, _, e in alerts] == ["check_failed", "check_failed"]


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


def test_the_safety_form_is_shown_with_the_saved_values(web, db, site):
    settings(db, rollback_on_failure=True, health_path="/up")
    page = web.get(f"/sites/{site.id}").text
    assert "Safety and alerts" in page and 'value="/up"' in page
    assert 'name="rollback_on_failure" value="1" style="width:auto;margin:0;" checked' in page
    assert 'value="https://hook.example.com/x"' in page and "Send a test message" in page


def test_saving_the_safety_settings(web, db, site):
    response = web.post(f"/sites/{site.id}/deploy/safety", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), "health_path": "/health", "rollback_on_failure": "1",
        "notify_url": "https://discord.com/api/webhooks/1/abc", "notify_success": "1",
    })
    assert response.status_code == 303 and response.headers["location"].endswith("#deploy-details")
    db.expire_all()
    dep = db.scalars(select(SiteDeployment)).first()
    assert (dep.health_path, dep.rollback_on_failure, dep.notify_url, dep.notify_success) == (
        "/health", True, "https://discord.com/api/webhooks/1/abc", True)


def test_clearing_the_url_turns_alerts_off_and_unchecked_boxes_turn_off(web, db, site):
    web.post(f"/sites/{site.id}/deploy/safety", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), "health_path": "/", "notify_url": "", "notify_success": "1",
    })
    db.expire_all()
    dep = db.scalars(select(SiteDeployment)).first()
    assert dep.notify_url is None and dep.notify_success is False and dep.rollback_on_failure is False


@pytest.mark.parametrize("field,value", [("health_path", "no-slash"), ("notify_url", "http://insecure.example.com/x")])
def test_bad_safety_values_are_rejected(web, db, site, field, value):
    data = {"csrf_token": _csrf(web, f"/sites/{site.id}"), "health_path": "/", "notify_url": ""}
    data[field] = value
    response = web.post(f"/sites/{site.id}/deploy/safety", data=data)
    assert "error=" in response.headers["location"]


def test_the_test_message_reports_success_or_the_reason_it_failed(web, db, site, monkeypatch):
    token = _csrf(web, f"/sites/{site.id}")
    sent = []
    monkeypatch.setattr(notify_service, "send", lambda url, text, **kw: sent.append(text))
    ok = web.post(f"/sites/{site.id}/deploy/notify-test", data={"csrf_token": token})
    assert "notice=" in ok.headers["location"] and sent and "test message" in sent[0]

    def refuse(url, text, **kw):
        raise notify_service.NotifyError("hook.example.com points to a private address (10.0.0.1)")

    monkeypatch.setattr(notify_service, "send", refuse)
    bad = web.post(f"/sites/{site.id}/deploy/notify-test", data={"csrf_token": token})
    assert "error=" in bad.headers["location"] and "private" in bad.headers["location"]


def test_the_test_message_needs_a_saved_url(web, db, site):
    settings(db, notify_url=None)
    response = web.post(f"/sites/{site.id}/deploy/notify-test", data={"csrf_token": _csrf(web, f"/sites/{site.id}")})
    assert "error=" in response.headers["location"]


def test_history_shows_the_new_states_and_the_health_result(web, db, site):
    db.add(SiteDeploy(site_id=site.id, status="rolled_back", commit="a" * 40, message="bad", health="HTTP 500"))
    db.add(SiteDeploy(site_id=site.id, status="success", commit="b" * 40, trigger="rollback", health="HTTP 200"))
    db.commit()
    page = web.get(f"/sites/{site.id}").text
    assert "rolled back" in page and "HTTP 500" in page and "rollback" in page


def test_existing_databases_get_the_safety_columns(tmp_path):
    import sqlite3

    from sqlalchemy import create_engine

    from app import database
    from app.models import Base

    path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for column in ("health_path", "rollback_on_failure", "notify_url", "notify_success"):
            conn.exec_driver_sql(f"ALTER TABLE site_deployments DROP COLUMN {column}")
        conn.exec_driver_sql("ALTER TABLE site_deploys DROP COLUMN health")
    original = database.engine
    database.engine = engine
    try:
        database._ensure_additive_columns()
    finally:
        database.engine = original
    con = sqlite3.connect(path)
    deployments = {row[1] for row in con.execute("PRAGMA table_info(site_deployments)")}
    deploys = {row[1] for row in con.execute("PRAGMA table_info(site_deploys)")}
    con.close()
    assert {"health_path", "rollback_on_failure", "notify_url", "notify_success"} <= deployments
    assert "health" in deploys
