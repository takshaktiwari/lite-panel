"""Git deployments: pull a site's code and run its deploy script.

Deliberately simple: the repository is checked out *in place* in the site's
project folder (no release directories or symlink switching).  A deploy is

    git fetch  ->  git checkout -f -B <branch> origin/<branch>   (or a given commit)
    -> the site's deploy script, line by line, stopping at the first failure
    -> restart the app / running workers, reload PHP, re-grant nginx access

and everything that touches the code runs as the site's own user, never root.
Untracked files (``.env``, ``vendor/``, ``node_modules/``, uploads) are never
removed, so connecting a folder that already holds a working site is safe.

Private repositories use a per-site read-only *deploy key*: the panel creates
an ed25519 key in the site's ``~/.ssh`` and shows the public half to paste into
GitHub / GitLab.  No password or token is ever stored.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from datetime import timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.models import SiteDeploy, SiteDeployment, utcnow
from app.providers import get_provider
from app.services import apps as apps_service
from app.services import notify as notify_service
from app.services import permissions as permissions_service
from app.services import workers as workers_service
from app.shell import run, which
from app.validators import ValidationError, is_ssh_repo_url, resolve_within

logger = logging.getLogger(__name__)

KEY_NAME = "lite_panel_deploy"
SCRIPT_TIMEOUT = 3600
GIT_TIMEOUT = 600
REMOTE_CHECK_TIMEOUT = 60
# A deploy that has been "running" longer than this is a stale row (the panel
# was restarted mid-deploy); it must not lock the site forever.
STALE_DEPLOY_AFTER = timedelta(hours=2)
POLL_TICK_SECONDS = 30
HEALTH_ATTEMPTS = 5      # the app / PHP may need a few seconds after a restart
HEALTH_DELAY_SECONDS = 3
HEALTH_FIRST_WAIT_SECONDS = 1
RECOMMENDED_POLL_MINUTES = 5

PRESETS: Dict[str, Dict[str, str]] = {
    "laravel": {
        "label": "Laravel",
        "script": (
            "composer install --no-dev --optimize-autoloader --no-interaction\n"
            "{PHP} artisan migrate --force\n"
            "{PHP} artisan optimize"
        ),
    },
    "php": {
        "label": "PHP with Composer",
        "script": "composer install --no-dev --optimize-autoloader --no-interaction",
    },
    "node": {"label": "Node.js", "script": "npm ci\nnpm run build --if-present"},
    "python": {
        "label": "Python",
        "script": "[ -d .venv ] || python3 -m venv .venv\npip install -r requirements.txt",
    },
    "static": {"label": "Static site (nothing to build)", "script": ""},
}


class DeployError(RuntimeError):
    """A deploy step failed; the message says which and why."""


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------


def default_preset(site) -> str:
    if site.app_type in ("node", "python"):
        return site.app_type
    if not site.php_version:
        return "static"
    return "laravel" if Path(site.webroot).name == "public" else "php"


def default_repo_dir(site) -> Path:
    """The project folder: the one above ``public`` for Laravel-style sites,
    the app folder for Node / Python, otherwise the document path."""
    return workers_service.workdir(site)


def resolve_repo_dir(site, value: str) -> Path:
    """A repo folder typed by the operator, kept inside the site's home."""
    return resolve_within(site.root_dir, (value or "").strip() or str(default_repo_dir(site)))


def new_webhook_token() -> str:
    return secrets.token_urlsafe(24)


def new_webhook_secret() -> str:
    return secrets.token_hex(20)


# --------------------------------------------------------------------------
# Deploy key
# --------------------------------------------------------------------------


def ssh_dir(site) -> Path:
    return Path(site.root_dir) / ".ssh"


def key_path(site) -> Path:
    return ssh_dir(site) / KEY_NAME


def ensure_deploy_key(site) -> str:
    """Create the site's deploy key if needed; return the public half.

    ssh refuses a private key that anyone else can read, and the site folder
    carries a read ACL for nginx -- so ``~/.ssh`` is stripped of every ACL and
    kept 700 / 600, owned by the site user.
    """
    key = key_path(site)
    public = key.with_suffix(".pub")
    directory = ssh_dir(site)
    if not key.exists():
        directory.mkdir(parents=True, exist_ok=True)
        result = run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
             f"lite-panel deploy {site.domain}", "-f", str(key)],
            check=False,
        )
        if not result.ok:
            raise DeployError(f"Could not create a deploy key: {(result.stderr or '').strip()[:200]}")
    run(["chown", "-R", f"{site.system_user}:{site.system_user}", str(directory)], check=False)
    if which("setfacl"):
        run(["setfacl", "-R", "-b", str(directory)], check=False)
        run(["setfacl", "-k", str(directory)], check=False)
    os.chmod(directory, 0o700)
    os.chmod(key, 0o600)
    if public.exists():
        os.chmod(public, 0o644)
        return public.read_text(encoding="utf-8").strip()
    raise DeployError("The deploy key was created without a public half.")


# --------------------------------------------------------------------------
# Commands (always as the site user)
# --------------------------------------------------------------------------


def _git_env(site) -> List[str]:
    known_hosts = ssh_dir(site) / "known_hosts"
    ssh = (
        f"ssh -i {key_path(site)} -o IdentitiesOnly=yes -o BatchMode=yes "
        f"-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={known_hosts}"
    )
    return [f"HOME={site.root_dir}", "GIT_TERMINAL_PROMPT=0", f"GIT_SSH_COMMAND={ssh}"]


def as_site_user(site, args: List[str], *, extra_env: Optional[List[str]] = None) -> List[str]:
    return ["runuser", "-u", site.system_user, "--", "env", *_git_env(site), *(extra_env or []), *args]


def git(site, repo: Path, *args: str) -> List[str]:
    return as_site_user(site, ["git", "-C", str(repo), *args])


def _check(ctx, args, what: str, **kwargs) -> None:
    if ctx.run(args, **kwargs) != 0:
        raise DeployError(f"{what} failed; see the lines above.")


def _label(site, *words: str) -> str:
    """What the job log shows for a command: ``$ git fetch origin``, not the
    runuser / env / ssh wrapper around it."""
    return f"({site.system_user}) " + " ".join(words)


# --------------------------------------------------------------------------
# Deploy
# --------------------------------------------------------------------------


def _prepare_folder(ctx, site, deployment, repo: Path) -> None:
    """Make ``repo`` a checkout of the deployment's repository.

    Empty folder -> clone.  Folder with files but no ``.git`` -> connect git to
    it in place (nothing is deleted).  Existing checkout -> just make sure
    ``origin`` points at the configured URL.
    """
    if not repo.exists():
        repo.mkdir(parents=True)
        run(["chown", f"{site.system_user}:{site.system_user}", str(repo)], check=False)

    branch = deployment.branch
    url = deployment.repo_url
    if (repo / ".git").exists():
        if ctx.run(git(site, repo, "remote", "set-url", "origin", url), label=_label(site, "git remote set-url origin", url)) != 0:
            _check(ctx, git(site, repo, "remote", "add", "origin", url), "Setting the git remote",
                   label=_label(site, "git remote add origin", url))
        return

    if not any(repo.iterdir()):
        ctx.log(f"Cloning {url} ({branch}) into {repo}")
        _check(
            ctx,
            as_site_user(site, ["git", "clone", "--branch", branch, "--", url, str(repo)]),
            "Cloning the repository",
            timeout=GIT_TIMEOUT,
            label=_label(site, "git clone --branch", branch, url, str(repo)),
        )
        return

    ctx.log(f"{repo} already has files: connecting git to it in place (nothing is deleted)")
    _check(ctx, git(site, repo, "init", "-q"), "git init", label=_label(site, "git init"))
    _check(ctx, git(site, repo, "remote", "add", "origin", url), "Adding the git remote",
           label=_label(site, "git remote add origin", url))


def _commit_info(site, repo: Path) -> Tuple[str, str]:
    result = run(git(site, repo, "log", "-1", "--format=%H%n%s"), check=False, timeout=60)
    lines = (result.stdout or "").splitlines()
    return (lines[0].strip() if lines else ""), (lines[1].strip() if len(lines) > 1 else "")


def resolved_script(site, script: str) -> str:
    if "{PHP}" in script:
        if not site.php_version:
            raise DeployError("The deploy script uses {PHP}, but the site has no PHP version.")
        script = script.replace("{PHP}", get_provider("php").cli_path_for(site.php_version))
    return script


def _run_script(ctx, site, deployment, repo: Path) -> None:
    script = (deployment.script or "").strip()
    if not script:
        ctx.log("No deploy script; nothing to run after the pull.")
        return
    text = resolved_script(site, script)

    scratch = Path(site.root_dir) / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / f"deploy-{int(time.time())}-{os.getpid()}.sh"
    path.write_text(text + "\n", encoding="utf-8")
    run(["chown", f"{site.system_user}:{site.system_user}", str(path)], check=False)
    os.chmod(path, 0o700)

    ctx.log("Running the deploy script (each command is shown with a + before it):")
    try:
        code = ctx.run(
            as_site_user(
                site,
                ["bash", "-e", "-o", "pipefail", "-x", str(path)],
                extra_env=[f"PATH={workers_service.path_env(site)}"],
            ),
            cwd=repo,
            timeout=SCRIPT_TIMEOUT,
            label=_label(site, "bash -e deploy script  (in", str(repo) + ")"),
        )
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if code != 0:
        raise DeployError(f"The deploy script stopped with exit code {code}; see the lines above.")


def _after_deploy(ctx, site) -> None:
    """Make the new code live: restart what holds the old code in memory."""
    if apps_service.is_app(site) and apps_service.state(site).active:
        apps_service.control(ctx, site, "restart")
    for worker in site.workers:
        if workers_service.state(site, worker).running:
            ctx.run(["systemctl", "restart", *workers_service.instance_names(site.name, worker.name, worker.processes)])
            ctx.log(f"Restarted worker {worker.name}")
    if site.php_version:
        # A reload is graceful and clears OPcache, so PHP serves the new files.
        ctx.run(["systemctl", "reload", get_provider("php").service_for(site.php_version)])
    try:
        permissions_service.apply(ctx, site)
    except permissions_service.PermissionsError as exc:
        ctx.log(f"note: {exc}")


def deploy(ctx, site, deployment, *, commit: Optional[str] = None) -> Tuple[str, str]:
    """Pull and run.  Returns (commit, message) of what is now live."""
    started = time.monotonic()
    repo = Path(deployment.repo_dir)
    ctx.log(f"Deploying {site.domain}: {deployment.repo_url} @ {commit[:7] if commit else deployment.branch}")
    ctx.log(f"Code folder: {repo}   (commands run as {site.system_user})")
    if is_ssh_repo_url(deployment.repo_url) and not key_path(site).exists():
        ensure_deploy_key(site)

    _prepare_folder(ctx, site, deployment, repo)

    branch = deployment.branch
    ctx.log(f"Fetching {branch} from {deployment.repo_url}")
    _check(
        ctx,
        git(site, repo, "fetch", "--prune", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"),
        "Fetching from the repository (is the deploy key added to the repo?)",
        timeout=GIT_TIMEOUT,
        label=_label(site, "git fetch --prune origin", branch),
    )
    target = commit or f"origin/{branch}"
    _check(ctx, git(site, repo, "checkout", "-q", "-f", "-B", branch, target), f"Checking out {target}",
           label=_label(site, "git checkout -f -B", branch, target))

    sha, message = _commit_info(site, repo)
    ctx.log(f"Now at {sha[:7]} {message}")

    _run_script(ctx, site, deployment, repo)
    _after_deploy(ctx, site)
    seconds = int(time.monotonic() - started)
    ctx.log(f"Deploy finished in {seconds // 60}m {seconds % 60:02d}s: {sha[:7]} {message}")
    return sha, message


# --------------------------------------------------------------------------
# Queueing and the per-site lock
# --------------------------------------------------------------------------


def active_deploy(db, site_id: int) -> Optional[SiteDeploy]:
    """A deploy of this site that is queued or running (and not a stale row)."""
    from sqlalchemy import select

    cutoff = utcnow() - STALE_DEPLOY_AFTER
    return db.scalars(
        select(SiteDeploy)
        .where(SiteDeploy.site_id == site_id)
        .where(SiteDeploy.status.in_(("queued", "running")))
        .where(SiteDeploy.started_at >= cutoff)
        .order_by(SiteDeploy.id.desc())
    ).first()


def queue_deploy(db, site, *, trigger: str, commit: Optional[str] = None, username=None, user_id=None):
    """Record a deploy in the history and hand it to the job worker.

    One deploy per site at a time.  Returns the ``Job``, or ``None`` when a
    deploy is already running and ``trigger`` is automatic (webhook / auto): that
    push is remembered (``rerun_pending``) and deployed right after the current
    one, so several quick pushes collapse into one more deploy of the newest
    commit.  A person clicking Deploy now while one runs gets an error instead.
    """
    from app.jobs import enqueue

    running = active_deploy(db, site.id)
    if running is not None:
        if trigger in ("webhook", "auto") and site.deployment is not None:
            site.deployment.rerun_pending = True
            db.commit()
            return None
        raise DeployError(f"A deploy is already running for {site.domain}; wait for it to finish.")

    record = SiteDeploy(
        site_id=site.id, trigger=trigger, requested_commit=commit, username=username, status="queued"
    )
    db.add(record)
    db.flush()
    label = f"Deploy {site.domain}" + (f" at {commit[:7]}" if commit else "")
    job = enqueue(
        db,
        "site.deploy",
        label,
        payload={"site_id": site.id, "deploy_id": record.id, "commit": commit},
        user_id=user_id,
    )
    record.job_id = job.id
    db.commit()
    return job


def mark_interrupted(db) -> int:
    """After a restart: deploys that were queued / running died with the old process."""
    from sqlalchemy import select

    stale = db.scalars(select(SiteDeploy).where(SiteDeploy.status.in_(("queued", "running")))).all()
    for record in stale:
        record.status = "failed"
        record.error = "The panel restarted while this deploy was running."
        record.finished_at = utcnow()
    return len(stale)


# --------------------------------------------------------------------------
# Polling: deploy when the branch moves
# --------------------------------------------------------------------------


def remote_head(site, deployment) -> str:
    """The commit the branch points to on the git host (``git ls-remote``)."""
    if is_ssh_repo_url(deployment.repo_url) and not key_path(site).exists():
        ensure_deploy_key(site)
    ref = f"refs/heads/{deployment.branch}"
    result = run(
        as_site_user(site, ["git", "ls-remote", "--", deployment.repo_url, ref]),
        check=False,
        timeout=REMOTE_CHECK_TIMEOUT,
    )
    if not result.ok:
        message = (result.stderr or result.stdout or "").strip().splitlines()
        raise DeployError((message[-1] if message else "git ls-remote failed")[:250])
    for line in (result.stdout or "").splitlines():
        sha, _, name = line.partition("\t")
        if name.strip() == ref and sha:
            return sha.strip()
    raise DeployError(f"Branch '{deployment.branch}' was not found in the repository.")


def local_head(site, deployment) -> Optional[str]:
    repo = Path(deployment.repo_dir)
    if not (repo / ".git").exists():
        return None
    result = run(git(site, repo, "rev-parse", "HEAD"), check=False, timeout=30)
    sha = (result.stdout or "").strip()
    return sha if result.ok and sha else None


def _naive_utc(moment):
    """SQLite hands datetimes back without a timezone while ``utcnow()`` is
    aware; compare them on the same footing (both UTC, no tzinfo)."""
    if moment is not None and moment.tzinfo is not None:
        return moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def poll_due(deployment, now) -> bool:
    if not deployment.poll_minutes:
        return False
    if deployment.last_checked_at is None:
        return True
    return _naive_utc(now) - _naive_utc(deployment.last_checked_at) >= timedelta(minutes=deployment.poll_minutes)


def poll_deployment(db, site, deployment, *, now=None) -> Optional[str]:
    """Check one site.  Returns "deployed" / "queued" / "current" / "error:...".

    A deploy is started only when the remote head *changed* since the last
    decision (or, on the very first check, differs from what is checked out).
    The head is recorded at decision time, so a failed deploy is not retried in
    a loop and a rollback is not undone: the next deploy waits for a new push.
    """
    now = now or utcnow()
    deployment.last_checked_at = now
    try:
        remote = remote_head(site, deployment)
    except (DeployError, ValidationError, OSError) as exc:
        first_failure = deployment.last_check_error is None
        deployment.last_check_error = str(exc)[:300]
        db.commit()
        if first_failure:      # once per streak, not every check
            alert(deployment, site, "check_failed",
                  f"cannot reach the repository to check for new commits: {str(exc)[:200]}", level="warn")
        return f"error: {exc}"
    deployment.last_check_error = None

    previous = deployment.last_remote_head
    if previous is None:
        local = local_head(site, deployment)
        if local is None:                       # never deployed: the first deploy is manual
            deployment.last_remote_head = remote
            db.commit()
            return "current"
        changed = local != remote
    else:
        changed = remote != previous
    deployment.last_remote_head = remote
    db.commit()
    if not changed:
        return "current"

    job = queue_deploy(db, site, trigger="auto", username="auto-deploy")
    return "queued" if job is not None else "pending"


def poll_once(*, now=None) -> int:
    """One pass over every deployment that is due.  Returns how many were checked."""
    from sqlalchemy import select

    from app.database import session_scope

    now = now or utcnow()
    checked = 0
    with session_scope() as db:
        for deployment in db.scalars(select(SiteDeployment).where(SiteDeployment.poll_minutes > 0)).all():
            if not poll_due(deployment, now):
                continue
            site = deployment.site
            try:
                result = poll_deployment(db, site, deployment, now=now)
                checked += 1
                if result not in ("current",):
                    logger.info("deploy poll %s: %s", site.domain, result)
            except Exception:  # noqa: BLE001 - one site must never stop the others
                logger.exception("deploy poll failed for %s", site.domain)
                db.rollback()
    return checked


_poller_thread: Optional[threading.Thread] = None
_poller_stop = threading.Event()


def _poller_loop() -> None:
    while not _poller_stop.wait(POLL_TICK_SECONDS):
        try:
            poll_once()
        except Exception:  # noqa: BLE001
            logger.exception("deploy poller pass failed")


def start_poller() -> None:
    global _poller_thread
    if _poller_thread and _poller_thread.is_alive():
        return
    _poller_stop.clear()
    _poller_thread = threading.Thread(target=_poller_loop, daemon=True, name="deploy-poller")
    _poller_thread.start()


def stop_poller() -> None:
    _poller_stop.set()
    if _poller_thread and _poller_thread.is_alive():
        _poller_thread.join(timeout=3)


# --------------------------------------------------------------------------
# Health check after a deploy, and rolling back
# --------------------------------------------------------------------------


class HealthResult:
    def __init__(self, ok: bool, text: str, status: Optional[int] = None) -> None:
        self.ok = ok
        self.text = text          # "HTTP 200" / "HTTP 500" / "no answer"
        self.status = status


def check_health(ctx, site, deployment) -> HealthResult:
    """Request the site (through nginx, with its Host header) until it answers.

    403 and 5xx mean broken; anything else, a redirect or a 404 included, means
    the site is up.  A new deploy needs a moment to come back, so an unhealthy
    answer is retried a few times before it counts.  If nginx does not answer
    at all the check cannot tell, which is not treated as a failure.
    """
    path = deployment.health_path or "/"
    last: Optional[int] = None
    for attempt in range(1, HEALTH_ATTEMPTS + 1):
        time.sleep(HEALTH_DELAY_SECONDS if attempt > 1 else HEALTH_FIRST_WAIT_SECONDS)
        last = permissions_service.http_status(site.domain, https=bool(site.ssl_enabled), path=path)
        if last is None:
            ctx.log("Health check: nginx did not answer a local request, so it could not be verified.")
            return HealthResult(True, "not verified")
        if last != 403 and last < 500:
            ctx.log(f"Health check: {path} answered HTTP {last}")
            return HealthResult(True, f"HTTP {last}", last)
        ctx.log(f"Health check {attempt}/{HEALTH_ATTEMPTS}: {path} answered HTTP {last}")
    return HealthResult(False, f"HTTP {last}", last)


def rollback_target(db, site, record) -> Optional[str]:
    """The newest earlier deploy that worked and is not the broken commit."""
    from sqlalchemy import select

    rows = db.scalars(
        select(SiteDeploy)
        .where(SiteDeploy.site_id == site.id)
        .where(SiteDeploy.status == "success")
        .where(SiteDeploy.id != record.id)
        .where(SiteDeploy.commit.is_not(None))
        .order_by(SiteDeploy.id.desc())
    ).all()
    for row in rows:
        if row.commit != record.commit:
            return row.commit
    return None


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------

_EMOJI = {"ok": "\u2705", "warn": "\u26a0\ufe0f", "bad": "\u274c"}


def alert(deployment, site, event: str, text: str, *, level: str = "bad", log=None) -> bool:
    """Tell the configured webhook.  Never raises: an alert must not break a deploy."""
    url = getattr(deployment, "notify_url", None)
    if not url:
        return False
    if level == "ok" and not deployment.notify_success:
        return False
    say = log or (lambda line: logger.info("%s", line))
    try:
        notify_service.send(url, f"{_EMOJI.get(level, '')} {site.domain}: {text}", event=event, site=site.domain)
    except notify_service.NotifyError as exc:
        say(f"Could not send the alert: {exc}")
        return False
    say("Alert sent.")
    return True
