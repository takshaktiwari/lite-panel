"""Node.js and Python sites: a process under systemd behind an nginx proxy.

A PHP site is "files plus a pool"; an application site is "files plus a
long-running process".  The panel owns that process the same way it owns an
FPM pool: one systemd unit per site, rendered from the database, running as
the site's own unprivileged user.  nginx (see ``vhost.conf.j2``) terminates
TLS and proxies to ``127.0.0.1:<app_port>``; the process itself never
listens publicly.

Everything is derived from the ``Site`` row (``app_type``, ``app_command``,
``app_port``, ``app_env``), so ``lite-panel rebuild`` can recreate the unit
and environment file from the database like any other rendered config.
"""

from __future__ import annotations

import logging
import re
import socket
import time
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.config import get_settings
from app.models import Site
from app.providers import get_provider
from app.services import logs as logs_service
from app.services import renderer, system
from app.shell import run, which
from app.validators import (
    RUNTIME_APP_TYPES,
    ValidationError,
    parse_app_env,
    validate_app_command,
    validate_app_env,
    validate_app_port,
    validate_app_type,
)

logger = logging.getLogger(__name__)

UNIT_DIR = Path("/etc/systemd/system")
UNIT_PREFIX = "lite-panel-app-"
PORT_RANGE_START = 3001
PORT_RANGE_END = 3999

RUNTIMES: Dict[str, Dict[str, str]] = {
    "node": {
        "label": "Node.js",
        "provider": "nodejs",
        "command": "npm start",
        "hint": "Make sure your app listens on the PORT environment variable.",
    },
    "python": {
        "label": "Python",
        "provider": "python",
        "command": "gunicorn app:app --bind 127.0.0.1:{PORT}",
        "hint": "Add gunicorn (or uvicorn) to requirements.txt; use {PORT} for the port.",
    },
}

# Suggestions offered in the UI.  They only pre-fill the start command; any
# command is accepted.
COMMAND_PRESETS: Dict[str, List[Dict[str, str]]] = {
    "node": [
        {"label": "npm start", "command": "npm start"},
        {"label": "node server.js", "command": "node server.js"},
        {"label": "node index.js", "command": "node index.js"},
        {"label": "Next.js", "command": "npm run start -- -p {PORT} -H 127.0.0.1"},
    ],
    "python": [
        {"label": "Gunicorn (Flask / Django)", "command": "gunicorn app:app --bind 127.0.0.1:{PORT}"},
        {"label": "Uvicorn (FastAPI)", "command": "uvicorn main:app --host 127.0.0.1 --port {PORT}"},
        {"label": "Django (wsgi)", "command": "gunicorn project.wsgi --bind 127.0.0.1:{PORT}"},
        {"label": "python app.py", "command": "python app.py"},
    ],
}

_UNIT_NAME = re.compile(r"^lite-panel-app-[a-z0-9_-]+\.service$")


# --------------------------------------------------------------------------
# Identity and paths
# --------------------------------------------------------------------------


def is_app(site) -> bool:
    return getattr(site, "app_type", "web") in RUNTIME_APP_TYPES


def runtime_label(app_type: str) -> str:
    return RUNTIMES[app_type]["label"] if app_type in RUNTIMES else "PHP / static"


def unit_name(site_name: str) -> str:
    return f"{UNIT_PREFIX}{site_name}.service"


def is_app_unit(name: str) -> bool:
    return bool(_UNIT_NAME.match(name or ""))


def unit_path(site_name: str) -> Path:
    return UNIT_DIR / unit_name(site_name)


def env_dir() -> Path:
    return get_settings().config_dir / "apps"


def env_path(site_name: str) -> Path:
    return env_dir() / f"{site_name}.env"


def workdir(site) -> Path:
    """Where the app's start command runs: the site's document path."""
    return Path(site.webroot)


def venv_dir(site) -> Path:
    return workdir(site) / ".venv"


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------


def require_runtime(app_type: str) -> None:
    """Refuse to create/run an app whose runtime isn't installed."""
    info = RUNTIMES[app_type]
    if not get_provider(info["provider"]).is_installed():
        raise ValidationError(
            f"{info['label']} isn't installed yet. Install it from the Stack page first."
        )


def default_command(app_type: str) -> str:
    return RUNTIMES[app_type]["command"]


def port_in_use_locally(port: int) -> bool:
    """Whether something already listens on 127.0.0.1:port."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            return probe.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def taken_ports(db: OrmSession, *, exclude_site_id: Optional[int] = None) -> set:
    rows = db.execute(select(Site.id, Site.app_port).where(Site.app_port.is_not(None))).all()
    return {port for site_id, port in rows if site_id != exclude_site_id}


def allocate_port(db: OrmSession) -> int:
    """The lowest free port in the panel's application range."""
    used = taken_ports(db)
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if port not in used and not port_in_use_locally(port):
            return port
    raise ValidationError("No free application ports are left.")


def check_port_free(db: OrmSession, port: int, *, site: Optional[Site] = None) -> int:
    port = validate_app_port(port)
    if port in taken_ports(db, exclude_site_id=site.id if site else None):
        raise ValidationError(f"Port {port} is already used by another site.")
    return port


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def systemd_quote(command: str) -> str:
    """Escape a command for a double-quoted systemd argument.

    ``%`` is a systemd specifier and ``$`` is expanded by systemd itself, so
    both are doubled; the command reaches bash intact and bash does its own
    ``$PORT`` expansion.
    """
    return (
        command.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )


def resolved_command(site) -> str:
    return (site.app_command or default_command(site.app_type)).replace(
        "{PORT}", str(site.app_port)
    )


def render_env_file(site) -> str:
    lines = ["# " + renderer.BANNER]
    for key, value in parse_app_env(site.app_env or ""):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    return "\n".join(lines) + "\n"


def path_env(site) -> str:
    tool_dir = (
        venv_dir(site) / "bin" if site.app_type == "python" else workdir(site) / "node_modules" / ".bin"
    )
    return f"{tool_dir}:/usr/local/bin:/usr/bin:/bin"


def render_unit(site) -> str:
    base_env = []
    if site.app_type == "node":
        base_env.append(("NODE_ENV", "production"))
    else:
        base_env.append(("PYTHONUNBUFFERED", "1"))
        base_env.append(("VIRTUAL_ENV", str(venv_dir(site))))
    return renderer.render(
        "app.service.j2",
        {
            "site": site,
            "runtime": runtime_label(site.app_type),
            "workdir": str(workdir(site)),
            "port": site.app_port,
            "path_env": path_env(site),
            "base_env": base_env,
            "env_file": str(env_path(site.name)),
            "exec_start": systemd_quote(resolved_command(site)),
        },
    )


def _systemctl(args: List[str], *, check: bool = False) -> None:
    if not which("systemctl"):
        return
    run(["systemctl", *args], check=check, timeout=60)


def render_files(site) -> bool:
    """Write the unit and environment file; reload systemd if the unit moved.

    Returns True when the unit changed (so a running app needs a restart to
    pick it up).  The environment file is root-only: it holds secrets.
    """
    env_target = env_path(site.name)
    renderer.write_atomic(env_target, render_env_file(site), mode=0o600)

    unit = unit_path(site.name)
    changed = renderer.render_to_file_text(unit, render_unit(site))
    if changed:
        _systemctl(["daemon-reload"])
    return changed


def remove_files(site_name: str) -> None:
    renderer.remove_file(unit_path(site_name))
    renderer.remove_file(env_path(site_name))
    _systemctl(["daemon-reload"])


# --------------------------------------------------------------------------
# Process control
# --------------------------------------------------------------------------


def state(site) -> system.ServiceState:
    return system.service_state(unit_name(site.name))


def _settle_and_verify(ctx, site, wait: float) -> None:
    """After a start/restart, make sure the process actually stayed up.

    A start command that exits immediately (missing dependency, syntax
    error) leaves systemd in a restart loop, which looks like success for
    the first instant.  Waiting briefly and reading the state turns that
    into a failure the operator can see, with the log lines that explain it.
    """
    time.sleep(wait)
    current = state(site)
    if current.active:
        ctx.log(f"{site.domain} app is running on 127.0.0.1:{site.app_port}")
        return
    ctx.log(f"The app did not stay running (state: {current.detail or 'unknown'}). Recent output:")
    ctx.log(logs_service.tail_journal(unit_name(site.name), 25))
    raise RuntimeError(
        "The app exited right after starting. Check the start command and "
        "that dependencies are installed, then try again."
    )


def control(ctx, site, action: str, *, wait: float = 2.0) -> None:
    unit = unit_name(site.name)
    if action not in ("start", "stop", "restart"):
        raise ValidationError("Unknown action.")
    if not unit_path(site.name).exists():
        render_files(site)

    if action == "stop":
        ctx.check(["systemctl", "disable", "--now", unit], timeout=60)
        ctx.log(f"{site.domain} app stopped")
        return

    require_runtime(site.app_type)
    if action == "start":
        ctx.check(["systemctl", "enable", "--now", unit], timeout=60)
    else:
        ctx.check(["systemctl", "enable", unit], timeout=60)
        ctx.check(["systemctl", "restart", unit], timeout=60)
    _settle_and_verify(ctx, site, wait)


def teardown(ctx, site) -> None:
    """Stop and forget a site's process (site deletion)."""
    unit = unit_name(site.name)
    if which("systemctl"):
        ctx.run(["systemctl", "disable", "--now", unit])
    remove_files(site.name)
    ctx.log(f"removed app service {unit}")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def update_settings(
    db: OrmSession,
    ctx,
    site: Site,
    *,
    command: str,
    port,
    env_text: str,
) -> None:
    """Change an application's command, port and environment, then restart
    it if it was running so the change takes effect."""
    if not is_app(site):
        raise ValidationError("This site isn't a Node.js or Python application.")

    command = validate_app_command(command)
    port = check_port_free(db, port, site=site)
    env_text = validate_app_env(env_text)

    was_active = state(site).active
    site.app_command = command
    site.app_port = port
    site.app_env = env_text
    db.flush()

    # Imported here: sites imports this module for its own rendering.
    from app.services import sites as sites_service

    sites_service.render_site(site)
    get_provider("nginx").reload(ctx)
    db.commit()
    ctx.log("Application settings saved")

    if was_active:
        control(ctx, site, "restart")
    else:
        ctx.log("The app isn't running; start it from the site page to apply them.")


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def _as_site_user(site, args: List[str]) -> List[str]:
    return ["runuser", "-u", site.system_user, "--", "env", f"HOME={site.root_dir}", *args]


def install_dependencies(ctx, site: Site, *, build: bool = False) -> None:
    """Install the app's packages as the site's own user."""
    if not is_app(site):
        raise ValidationError("This site isn't a Node.js or Python application.")
    require_runtime(site.app_type)

    directory = workdir(site)
    if not directory.is_dir():
        raise ValidationError(f"The app folder {directory} doesn't exist.")

    if site.app_type == "node":
        _install_node(ctx, site, directory, build)
    else:
        _install_python(ctx, site, directory)
    ctx.log("Dependencies are installed. Restart the app to use them.")


def _install_node(ctx, site: Site, directory: Path, build: bool) -> None:
    if not (directory / "package.json").is_file():
        raise ValidationError(f"No package.json found in {directory}. Upload your app first.")

    locked = (directory / "package-lock.json").is_file()
    ctx.log("Installing npm packages" + (" from package-lock.json" if locked else ""))
    ctx.check(
        _as_site_user(site, ["npm", "ci" if locked else "install"]),
        cwd=directory,
        timeout=1800,
    )

    if build:
        ctx.log("Running npm run build")
        ctx.check(_as_site_user(site, ["npm", "run", "build"]), cwd=directory, timeout=1800)


def _install_python(ctx, site: Site, directory: Path) -> None:
    venv = venv_dir(site)
    if not (venv / "bin" / "python").exists():
        ctx.log(f"Creating virtualenv at {venv}")
        ctx.check(_as_site_user(site, ["python3", "-m", "venv", str(venv)]), cwd=directory, timeout=300)
    else:
        ctx.log(f"Using existing virtualenv at {venv}")

    requirements = directory / "requirements.txt"
    if not requirements.is_file():
        ctx.log("No requirements.txt found; the virtualenv is ready but nothing was installed.")
        return
    ctx.log("Installing packages from requirements.txt")
    ctx.check(
        _as_site_user(site, [str(venv / "bin" / "pip"), "install", "-r", "requirements.txt"]),
        cwd=directory,
        timeout=1800,
    )


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------


def recent_logs(site, lines: int = 100) -> str:
    return logs_service.tail_journal(unit_name(site.name), lines)
