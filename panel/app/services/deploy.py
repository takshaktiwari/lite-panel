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
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.providers import get_provider
from app.services import apps as apps_service
from app.services import permissions as permissions_service
from app.services import workers as workers_service
from app.shell import run, which
from app.validators import ValidationError, is_ssh_repo_url, resolve_within

logger = logging.getLogger(__name__)

KEY_NAME = "lite_panel_deploy"
SCRIPT_TIMEOUT = 3600
GIT_TIMEOUT = 600

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
