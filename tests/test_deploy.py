"""Git deployments.

The deploy itself is exercised against *real* git: a bare repository in a temp
folder plays GitHub, and only ``runuser`` (switching to the site user) is
stripped.  So clone, connect-in-place, fetch, checkout, rollback, untracked
files surviving and the script stopping at a failure are all real behaviour.
The restart / reload / permission steps after a deploy are recorded instead.
"""

import hashlib
import hmac
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
from app.shell import stream
from app.validators import (
    ValidationError,
    validate_branch,
    validate_commit,
    validate_deploy_script,
    validate_repo_url,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# --------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "git@github.com:you/app.git",
    "git@gitlab.com:group/sub/app.git",
    "ssh://git@example.com:2222/you/app.git",
    "https://github.com/you/app.git",
    "https://github.com/you/app",
])
def test_repo_url_accepts_ssh_and_plain_https(url):
    assert validate_repo_url(url) == url


@pytest.mark.parametrize("url", [
    "", "https://user:pass@github.com/you/app.git", "http://github.com/you/app.git",
    "-oProxyCommand=x@host:repo", "git@github.com:you/app.git; rm -rf /", "file:///etc",
    "ext::sh -c id", "git@github.com:you/app.git\nx",
])
def test_repo_url_rejects_credentials_options_and_odd_transports(url):
    with pytest.raises(ValidationError):
        validate_repo_url(url)


def test_branch_rules():
    assert validate_branch("") == "main"
    assert validate_branch("release/1.2") == "release/1.2"
    for bad in ("-x", "a..b", "a b", "x.lock", "x/", "@{u}", "a//b"):
        with pytest.raises(ValidationError):
            validate_branch(bad)


def test_commit_and_script_rules():
    assert validate_commit("ABCDEF1") == "abcdef1"
    with pytest.raises(ValidationError):
        validate_commit("HEAD~1")
    assert validate_deploy_script("a\r\nb\r\n") == "a\nb"
    with pytest.raises(ValidationError):
        validate_deploy_script("x" * 20001)


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------


def _site(tmp_path, **overrides):
    root = tmp_path / "site"
    root.mkdir(exist_ok=True)
    values = dict(
        id=1, name="shop", domain="shop.example.com", system_user="site_shop",
        root_dir=str(root), webroot=str(root / "public"), php_version="8.3",
        app_type="web", app_port=None, workers=[], ssl_enabled=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_default_preset_and_folder_follow_the_site_type(tmp_path):
    assert deploy_service.default_preset(_site(tmp_path)) == "laravel"
    assert deploy_service.default_preset(_site(tmp_path, webroot=str(tmp_path / "site"))) == "php"
    assert deploy_service.default_preset(_site(tmp_path, php_version=None)) == "static"
    assert deploy_service.default_preset(_site(tmp_path, app_type="node")) == "node"
    assert deploy_service.default_repo_dir(_site(tmp_path)) == tmp_path / "site"   # above public/


def test_repo_folder_must_stay_inside_the_site(tmp_path):
    site = _site(tmp_path)
    assert deploy_service.resolve_repo_dir(site, "") == tmp_path / "site"
    with pytest.raises(ValidationError):
        deploy_service.resolve_repo_dir(site, "/etc")


# --------------------------------------------------------------------------
# Real git
# --------------------------------------------------------------------------


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """A bare repo with two commits on main, plus a working copy to push from."""
    bare = tmp_path / "origin.git"
    work = tmp_path / "work"
    sh("git", "init", "-q", "--bare", "-b", "main", str(bare))
    sh("git", "clone", "-q", str(bare), str(work))
    for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
        sh("git", "config", key, value, cwd=work)
    sh("git", "checkout", "-q", "-b", "main", cwd=work)

    def commit(name, content, message):
        (work / name).write_text(content)
        sh("git", "add", ".", cwd=work)
        sh("git", "commit", "-q", "-m", message, cwd=work)
        sh("git", "push", "-q", "origin", "main", cwd=work)
        return sh("git", "rev-parse", "HEAD", cwd=work)

    first = commit("index.html", "v1", "first version")
    second = commit("index.html", "v2", "second version")
    return SimpleNamespace(url=str(bare), work=work, first=first, second=second, commit=commit)


class RealCtx:
    def __init__(self):
        self.lines = []

    def log(self, line):
        self.lines.append(line)

    def run(self, args, **kwargs):
        label = kwargs.pop("label", None)
        self.lines.append("$ " + (label or " ".join(map(str, args))))
        return stream(args, self.lines.append, **kwargs)

    def check(self, args, **kwargs):
        assert self.run(args, **kwargs) == 0


@pytest.fixture
def real_git(monkeypatch):
    """Run git as the current user instead of switching to the site user."""
    monkeypatch.setattr(
        deploy_service, "as_site_user",
        lambda site, args, extra_env=None: ["env", *deploy_service._git_env(site), *(extra_env or []), *args],
    )
    after = []
    monkeypatch.setattr(deploy_service, "_after_deploy", lambda ctx, site: after.append(site.name))
    return after


def _deployment(site, url, script="", branch="main"):
    return SimpleNamespace(repo_url=url, branch=branch, repo_dir=site.root_dir, script=script)


def test_first_deploy_clones_into_an_empty_folder(tmp_path, origin, real_git):
    site = _site(tmp_path)
    sha, message = deploy_service.deploy(RealCtx(), site, _deployment(site, origin.url))
    assert sha == origin.second and message == "second version"
    assert (Path(site.root_dir) / "index.html").read_text() == "v2"
    assert real_git == ["shop"]                   # restart / reload / permissions ran


def test_existing_files_are_connected_in_place_and_untracked_files_survive(tmp_path, origin, real_git):
    site = _site(tmp_path)
    root = Path(site.root_dir)
    (root / "index.html").write_text("uploaded by ftp")
    (root / ".env").write_text("SECRET=1")
    (root / "storage").mkdir()
    (root / "storage" / "upload.jpg").write_text("photo")

    deploy_service.deploy(RealCtx(), site, _deployment(site, origin.url))

    assert (root / "index.html").read_text() == "v2"          # tracked: repo version
    assert (root / ".env").read_text() == "SECRET=1"           # untracked: untouched
    assert (root / "storage" / "upload.jpg").read_text() == "photo"


def test_next_deploy_pulls_new_commits_and_keeps_local_untracked_files(tmp_path, origin, real_git):
    site = _site(tmp_path)
    dep = _deployment(site, origin.url)
    deploy_service.deploy(RealCtx(), site, dep)
    (Path(site.root_dir) / "vendor").mkdir()
    third = origin.commit("index.html", "v3", "third version")
    (Path(site.root_dir) / "index.html").write_text("edited on the server")  # a stray local edit

    sha, _ = deploy_service.deploy(RealCtx(), site, dep)

    assert sha == third
    assert (Path(site.root_dir) / "index.html").read_text() == "v3"   # local edit cannot block a deploy
    assert (Path(site.root_dir) / "vendor").is_dir()


def test_redeploy_of_an_old_commit_rolls_back_and_the_next_deploy_returns(tmp_path, origin, real_git):
    site = _site(tmp_path)
    dep = _deployment(site, origin.url)
    deploy_service.deploy(RealCtx(), site, dep)

    sha, message = deploy_service.deploy(RealCtx(), site, dep, commit=origin.first)
    assert sha == origin.first and message == "first version"
    assert (Path(site.root_dir) / "index.html").read_text() == "v1"

    sha, _ = deploy_service.deploy(RealCtx(), site, dep)
    assert sha == origin.second


def test_the_script_runs_in_the_code_folder_and_stops_at_the_first_failure(tmp_path, origin, real_git):
    site = _site(tmp_path)
    script = "echo built > build.txt\nfalse\necho never > after.txt"
    ctx = RealCtx()
    with pytest.raises(deploy_service.DeployError, match="exit code"):
        deploy_service.deploy(ctx, site, _deployment(site, origin.url, script=script))
    root = Path(site.root_dir)
    assert (root / "build.txt").read_text().strip() == "built"
    assert not (root / "after.txt").exists()
    assert any(line.startswith("+ echo built") for line in ctx.lines)   # commands are echoed
    assert real_git == []                                               # nothing restarted
    assert not list((root / "tmp").glob("deploy-*.sh"))                 # script file cleaned up


def test_php_placeholder_is_replaced_and_required(tmp_path, origin, real_git, monkeypatch):
    fake = MagicMock()
    fake.cli_path_for.return_value = shutil.which("echo")
    monkeypatch.setattr(deploy_service, "get_provider", lambda key: fake)
    site = _site(tmp_path)
    deploy_service.deploy(RealCtx(), site, _deployment(site, origin.url, script="{PHP} artisan > out.txt"))
    assert (Path(site.root_dir) / "out.txt").read_text().strip() == "artisan"

    with pytest.raises(deploy_service.DeployError, match="no PHP version"):
        deploy_service.resolved_script(_site(tmp_path, php_version=None), "{PHP} x")


def test_a_wrong_branch_fails_with_a_readable_message(tmp_path, origin, real_git):
    site = _site(tmp_path)
    with pytest.raises(deploy_service.DeployError, match="failed"):
        deploy_service.deploy(RealCtx(), site, _deployment(site, origin.url, branch="nope"))


# --------------------------------------------------------------------------
# After a deploy
# --------------------------------------------------------------------------


def test_after_deploy_restarts_running_things_and_reloads_php(tmp_path, monkeypatch):
    calls = []
    ctx = SimpleNamespace(run=lambda args, **kw: calls.append(list(args)) or 0, log=lambda line: None)
    monkeypatch.setattr(deploy_service.apps_service, "is_app", lambda site: True)
    monkeypatch.setattr(deploy_service.apps_service, "state", lambda site: SimpleNamespace(active=True))
    monkeypatch.setattr(deploy_service.apps_service, "control", lambda ctx, site, action: calls.append(["app", action]))
    monkeypatch.setattr(
        deploy_service.workers_service, "state",
        lambda site, w: SimpleNamespace(running=1 if w.name == "queue" else 0),
    )
    php = MagicMock()
    php.service_for.return_value = "php8.3-fpm"
    monkeypatch.setattr(deploy_service, "get_provider", lambda key: php)
    granted = []
    monkeypatch.setattr(deploy_service.permissions_service, "apply", lambda ctx, site: granted.append(True))
    workers = [SimpleNamespace(name="queue", processes=2), SimpleNamespace(name="horizon", processes=1)]
    deploy_service._after_deploy(ctx, _site(tmp_path, workers=workers))
    assert ["app", "restart"] in calls
    assert ["systemctl", "restart", "lite-panel-worker-shop.queue@1.service", "lite-panel-worker-shop.queue@2.service"] in calls
    assert not any("horizon" in " ".join(c) for c in calls)          # stopped worker stays stopped
    assert ["systemctl", "reload", "php8.3-fpm"] in calls
    assert granted == [True]


# --------------------------------------------------------------------------
# Deploy key
# --------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen missing")
def test_deploy_key_is_created_once_private_and_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_service, "which", lambda name: None)      # no setfacl here
    site = _site(tmp_path)
    public = deploy_service.ensure_deploy_key(site)
    assert public.startswith("ssh-ed25519 ") and "lite-panel deploy shop.example.com" in public
    key = deploy_service.key_path(site)
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    assert oct(key.parent.stat().st_mode & 0o777) == "0o700"
    assert deploy_service.ensure_deploy_key(site) == public                # not regenerated


def test_git_uses_the_deploy_key_and_never_prompts(tmp_path):
    env = deploy_service._git_env(_site(tmp_path))
    assert "GIT_TERMINAL_PROMPT=0" in env
    ssh = next(e for e in env if e.startswith("GIT_SSH_COMMAND="))
    assert "lite_panel_deploy" in ssh and "BatchMode=yes" in ssh and "IdentitiesOnly=yes" in ssh


def test_commands_switch_to_the_site_user():
    site = SimpleNamespace(system_user="site_shop", root_dir="/var/www/shop")
    cmd = deploy_service.git(site, Path("/var/www/shop"), "fetch")
    assert cmd[:4] == ["runuser", "-u", "site_shop", "--"]
    assert cmd[-4:] == ["git", "-C", "/var/www/shop", "fetch"]


# --------------------------------------------------------------------------
# Job: history is recorded either way
# --------------------------------------------------------------------------


def _db_site(db, tmp_path, with_deployment=True):
    root = tmp_path / "shop"
    root.mkdir(exist_ok=True)
    site = Site(name="shop", domain="shop.example.com", system_user="site_shop",
                root_dir=str(root), webroot=str(root / "public"), php_version="8.3")
    db.add(site)
    db.flush()
    if with_deployment:
        db.add(SiteDeployment(site_id=site.id, repo_url="git@github.com:you/app.git", branch="main",
                              repo_dir=str(root), script="", webhook_token="tok-abc", webhook_secret="s3cret"))
    db.commit()
    return site


class JobCtx:
    def __init__(self, payload):
        self.payload = payload
        self.lines = []

    def log(self, line):
        self.lines.append(line)


def test_job_records_a_successful_deploy(db, tmp_path, monkeypatch):
    site = _db_site(db, tmp_path)
    record = SiteDeploy(site_id=site.id, trigger="manual")
    db.add(record)
    db.commit()
    monkeypatch.setattr(deploy_service, "deploy", lambda ctx, s, d, commit=None: ("a" * 40, "fix login"))
    tasks.deploy_site(JobCtx({"site_id": site.id, "deploy_id": record.id, "commit": None}))
    db.expire_all()
    saved = db.get(SiteDeploy, record.id)
    assert (saved.status, saved.commit, saved.message) == ("success", "a" * 40, "fix login")
    assert saved.finished_at is not None and saved.short_commit == "aaaaaaa"


def test_job_records_a_failed_deploy_and_fails(db, tmp_path, monkeypatch):
    site = _db_site(db, tmp_path)
    record = SiteDeploy(site_id=site.id, trigger="webhook")
    db.add(record)
    db.commit()

    def boom(ctx, s, d, commit=None):
        raise deploy_service.DeployError("The deploy script stopped with exit code 1")

    monkeypatch.setattr(deploy_service, "deploy", boom)
    with pytest.raises(jobs.JobFailed, match="exit code 1"):
        tasks.deploy_site(JobCtx({"site_id": site.id, "deploy_id": record.id}))
    db.expire_all()
    saved = db.get(SiteDeploy, record.id)
    assert saved.status == "failed" and "exit code 1" in saved.error


# --------------------------------------------------------------------------
# Web
# --------------------------------------------------------------------------


@pytest.fixture
def web(monkeypatch, db, admin):
    monkeypatch.setattr(jobs.worker, "submit", lambda job_id: None)
    fake = MagicMock()
    fake.is_installed.return_value = True
    fake.installed_versions.return_value = ["8.3"]
    monkeypatch.setattr("app.routers.sites.get_provider", lambda key: fake)
    monkeypatch.setattr(deploy_service, "ensure_deploy_key", lambda site: "ssh-ed25519 AAAAtest lite-panel deploy")
    with TestClient(create_app(), follow_redirects=False) as client:
        assert client.post("/login", data={"username": "admin", "password": "correct-horse-battery"}).status_code == 303
        yield client


def _csrf(client, path):
    page = client.get(path)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


def _last(db, model):
    return db.scalars(select(model).order_by(model.id.desc())).first()


def test_setup_form_is_shown_until_a_repository_is_saved(web, db, tmp_path):
    site = _db_site(db, tmp_path, with_deployment=False)
    page = web.get(f"/sites/{site.id}")
    assert 'id="deploy-panel"' in page.text and "Save and set up" in page.text
    assert "composer install" in page.text                    # Laravel preset pre-filled
    assert "Deploy now" not in page.text
    assert "/static/site-deploy.js" in page.text


def test_saving_settings_creates_the_deployment_and_a_deploy_key(web, db, tmp_path):
    site = _db_site(db, tmp_path, with_deployment=False)
    response = web.post(f"/sites/{site.id}/deploy/settings", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), "repo_url": "git@github.com:you/app.git",
        "branch": "main", "repo_dir": "", "script": "composer install\r\n",
    })
    assert response.status_code == 303
    dep = _last(db, SiteDeployment)
    assert dep.repo_dir == str(tmp_path / "shop") and dep.script == "composer install"
    assert dep.deploy_key_public.startswith("ssh-ed25519")
    assert len(dep.webhook_token) >= 24 and len(dep.webhook_secret) >= 32 and not dep.webhook_enabled

    page = web.get(f"/sites/{site.id}").text
    assert "Deploy now" in page and "ssh-ed25519 AAAAtest" in page and "Deploy keys" in page


def test_https_repos_get_no_deploy_key(web, db, tmp_path):
    site = _db_site(db, tmp_path, with_deployment=False)
    web.post(f"/sites/{site.id}/deploy/settings", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), "repo_url": "https://github.com/you/app.git",
        "branch": "main", "script": "",
    })
    assert _last(db, SiteDeployment).deploy_key_public is None


@pytest.mark.parametrize("field,value,message", [
    ("repo_url", "https://u:p@github.com/x.git", "Use an SSH URL"),
    ("branch", "a..b", "branch"),
    ("repo_dir", "/etc", ""),
])
def test_bad_settings_are_reported_on_the_page(web, db, tmp_path, field, value, message):
    site = _db_site(db, tmp_path, with_deployment=False)
    data = {"csrf_token": _csrf(web, f"/sites/{site.id}"), "repo_url": "git@github.com:you/app.git",
            "branch": "main", "script": ""}
    data[field] = value
    response = web.post(f"/sites/{site.id}/deploy/settings", data=data)
    assert "error=" in response.headers["location"]
    assert db.scalars(select(SiteDeployment)).all() == []


def test_deploy_now_queues_a_job_and_a_history_row(web, db, tmp_path):
    site = _db_site(db, tmp_path)
    response = web.post(f"/sites/{site.id}/deploy", data={"csrf_token": _csrf(web, f"/sites/{site.id}")})
    assert response.status_code == 303
    job = _last(db, Job)
    record = _last(db, SiteDeploy)
    assert job.kind == "site.deploy"
    assert json.loads(job.payload) == {"site_id": site.id, "deploy_id": record.id, "commit": None}
    assert (record.status, record.trigger, record.username, record.job_id) == ("queued", "manual", "admin", job.id)


def test_redeploy_passes_the_commit_and_validates_it(web, db, tmp_path):
    site = _db_site(db, tmp_path)
    token = _csrf(web, f"/sites/{site.id}")
    web.post(f"/sites/{site.id}/deploy", data={"csrf_token": token, "commit": "ABCDEF1234"})
    record = _last(db, SiteDeploy)
    assert record.trigger == "redeploy" and record.requested_commit == "abcdef1234"
    bad = web.post(f"/sites/{site.id}/deploy", data={"csrf_token": token, "commit": "HEAD~1"})
    assert "error=" in bad.headers["location"]


def test_deploy_without_settings_is_refused(web, db, tmp_path):
    site = _db_site(db, tmp_path, with_deployment=False)
    response = web.post(f"/sites/{site.id}/deploy", data={"csrf_token": _csrf(web, f"/sites/{site.id}")})
    assert "error=" in response.headers["location"]
    assert db.scalars(select(Job)).all() == []


def test_webhook_toggle_and_rotate(web, db, tmp_path):
    site = _db_site(db, tmp_path)
    token = _csrf(web, f"/sites/{site.id}")
    web.post(f"/sites/{site.id}/deploy/webhook", data={"csrf_token": token, "action": "enable"})
    dep = _last(db, SiteDeployment)
    db.refresh(dep)
    assert dep.webhook_enabled
    page = web.get(f"/sites/{site.id}").text
    assert "/hooks/deploy/tok-abc" in page and "s3cret" in page

    web.post(f"/sites/{site.id}/deploy/webhook", data={"csrf_token": token, "action": "rotate"})
    db.refresh(dep)
    assert dep.webhook_token != "tok-abc" and dep.webhook_secret != "s3cret"

    web.post(f"/sites/{site.id}/deploy/webhook", data={"csrf_token": token, "action": "disable"})
    db.refresh(dep)
    assert not dep.webhook_enabled


def test_disconnect_forgets_settings_but_keeps_history(web, db, tmp_path):
    site = _db_site(db, tmp_path)
    db.add(SiteDeploy(site_id=site.id, status="success", commit="a" * 40))
    db.commit()
    web.post(f"/sites/{site.id}/deploy/disconnect", data={"csrf_token": _csrf(web, f"/sites/{site.id}")})
    assert db.scalars(select(SiteDeployment)).all() == []
    assert len(db.scalars(select(SiteDeploy)).all()) == 1


def test_history_offers_redeploy_on_older_successful_deploys(web, db, tmp_path):
    site = _db_site(db, tmp_path)
    db.add(SiteDeploy(site_id=site.id, status="success", commit="1" * 40, message="older"))
    db.add(SiteDeploy(site_id=site.id, status="success", commit="2" * 40, message="newest"))
    db.commit()
    page = web.get(f"/sites/{site.id}").text
    assert 'value="' + "1" * 40 + '"' in page          # rollback target
    assert 'value="' + "2" * 40 + '"' not in page      # the live one has no redeploy


# --------------------------------------------------------------------------
# Webhook (public)
# --------------------------------------------------------------------------


def _signed(body: bytes, secret="s3cret"):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def hook(db, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.worker, "submit", lambda job_id: None)
    site = _db_site(db, tmp_path)
    dep = _last(db, SiteDeployment)
    dep.webhook_enabled = True
    db.commit()
    with TestClient(create_app(), follow_redirects=False) as client:
        yield client, site


def _push(client, body, **headers):
    return client.post("/hooks/deploy/tok-abc", content=body,
                       headers={"Content-Type": "application/json", **headers})


def test_a_signed_push_to_the_branch_deploys(db, hook):
    client, site = hook
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    response = _push(client, body, **{"X-GitHub-Event": "push", "X-Hub-Signature-256": _signed(body)})
    assert response.status_code == 202 and "queued" in response.json()
    record = _last(db, SiteDeploy)
    assert record.trigger == "webhook" and record.status == "queued"


def test_unsigned_or_badly_signed_requests_are_refused(db, hook):
    client, _ = hook
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    assert _push(client, body).status_code == 401
    assert _push(client, body, **{"X-Hub-Signature-256": _signed(body, "wrong")}).status_code == 401
    assert db.scalars(select(Job)).all() == []


def test_unknown_or_disabled_hooks_are_404(db, hook):
    client, _ = hook
    assert client.post("/hooks/deploy/nope", content=b"{}").status_code == 404
    dep = _last(db, SiteDeployment)
    dep.webhook_enabled = False
    db.commit()
    body = b'{"ref":"refs/heads/main"}'
    assert _push(client, body, **{"X-Hub-Signature-256": _signed(body)}).status_code == 404


def test_ping_and_other_branches_do_not_deploy(db, hook):
    client, _ = hook
    body = b'{"zen":"hi"}'
    assert _push(client, body, **{"X-GitHub-Event": "ping", "X-Hub-Signature-256": _signed(body)}).json()["message"] == "pong"
    other = b'{"ref":"refs/heads/feature"}'
    response = _push(client, other, **{"X-GitHub-Event": "push", "X-Hub-Signature-256": _signed(other)})
    assert response.status_code == 202 and "ignored" in response.json()
    assert db.scalars(select(Job)).all() == []


def test_gitlab_token_header_is_accepted(db, hook):
    client, _ = hook
    body = b'{"ref":"refs/heads/main"}'
    response = _push(client, body, **{"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": "s3cret"})
    assert response.status_code == 202
    assert _push(client, body, **{"X-Gitlab-Token": "nope"}).status_code == 401


def test_github_form_encoded_payloads_work(db, hook):
    from urllib.parse import urlencode

    client, _ = hook
    body = urlencode({"payload": json.dumps({"ref": "refs/heads/main"})}).encode()
    response = client.post("/hooks/deploy/tok-abc", content=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "X-GitHub-Event": "push", "X-Hub-Signature-256": _signed(body),
    })
    assert response.status_code == 202


# --------------------------------------------------------------------------
# Readable job log
# --------------------------------------------------------------------------


def test_the_log_shows_short_commands_not_the_runuser_env_ssh_wrapper(tmp_path, origin, real_git):
    site = _site(tmp_path)
    ctx = RealCtx()
    deploy_service.deploy(ctx, site, _deployment(site, origin.url, script="echo hi"))
    text = "\n".join(ctx.lines)
    assert "$ (site_shop) git clone --branch main" in text
    assert "GIT_SSH_COMMAND" not in text and "UserKnownHostsFile" not in text
    assert "Deploying shop.example.com:" in text and "commands run as site_shop" in text
    assert text.rstrip().splitlines()[-1].startswith("Deploy finished in 0m ")
    assert "+ echo hi" in text                                            # script lines still echoed


def test_job_log_strips_terminal_colour_codes():
    from app.jobs import JobContext, strip_ansi

    coloured = "  \x1b[37;44m INFO \x1b[39;49m Discovering packages.  \x1b[32;1mDONE\x1b[39;22m"
    assert strip_ansi(coloured) == "   INFO  Discovering packages.  DONE"
    assert strip_ansi("plain text, 100% [ok]") == "plain text, 100% [ok]"
    assert strip_ansi("\x1b[2K\x1b[1Gprogress") == "progress"

    ctx = JobContext(job_id=987654, payload={})
    ctx.log("\x1b[31;1merror:\x1b[0m failed")
    assert ctx._pending == ["error: failed"]


# --------------------------------------------------------------------------
# Folded section and the Overview button
# --------------------------------------------------------------------------


def test_once_set_up_only_the_header_buttons_show_and_the_rest_is_folded(web, db, tmp_path):
    site = _db_site(db, tmp_path)
    page = web.get(f"/sites/{site.id}").text
    assert 'id="deploy-details" hidden' in page                      # folded by default
    assert 'data-toggle-target="deploy-details"' in page             # the Details button
    assert 'data-toggle-label-show="Details" data-toggle-label-hide="Hide details"' in page
    # The folded block holds everything but the header, subtitle and buttons.
    head, folded = page.split('id="deploy-details" hidden', 1)
    assert "Deploy now" in head and "Pull the latest code from git" in head
    for text in ("Deploy key", "Auto-deploy on push", "History", "Settings and deploy script"):
        assert text in folded and text not in head.split('id="deploy-panel"', 1)[1]


def test_the_setup_form_is_not_folded_before_a_repository_is_connected(web, db, tmp_path):
    site = _db_site(db, tmp_path, with_deployment=False)
    page = web.get(f"/sites/{site.id}").text
    assert 'id="deploy-details"' not in page and "Save and set up" in page
    assert 'data-toggle-target="deploy-details"' not in page


def test_the_overview_has_a_deploy_button_only_when_deployment_is_set_up(web, db, tmp_path):
    site = _db_site(db, tmp_path)
    page = web.get(f"/sites/{site.id}").text
    overview = page.split("<h2>Overview</h2>", 1)[1].split("</section>", 1)[0]
    assert f'action="/sites/{site.id}/deploy"' in overview and ">Deploy</button>" in overview

    db.delete(_last(db, SiteDeployment))
    db.commit()
    overview = web.get(f"/sites/{site.id}").text.split("<h2>Overview</h2>", 1)[1].split("</section>", 1)[0]
    assert "/deploy" not in overview


def test_saving_settings_comes_back_with_the_details_open(web, db, tmp_path):
    site = _db_site(db, tmp_path, with_deployment=False)
    response = web.post(f"/sites/{site.id}/deploy/settings", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), "repo_url": "git@github.com:you/app.git",
        "branch": "main", "script": "",
    })
    assert response.headers["location"].endswith("#deploy-details")      # the deploy key is shown
    token = _csrf(web, f"/sites/{site.id}")
    response = web.post(f"/sites/{site.id}/deploy/webhook", data={"csrf_token": token, "action": "enable"})
    assert response.headers["location"].endswith("#deploy-details")
    assert "window.location.hash" in Path("panel/app/static/site-deploy.js").read_text()
