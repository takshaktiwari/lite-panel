"""Background workers: long-running processes of a site under systemd.

Laravel queue workers, Horizon, Reverb, Celery, a Node job runner -- anything
that must run continuously, restart when it crashes and come back after a
reboot.  The panel uses systemd for this (the same mechanism as Node/Python
apps in :mod:`app.services.apps`) rather than a second process manager.

One worker is one *template* unit, ``lite-panel-worker-<site>.<name>@.service``;
running ``processes`` copies of it means enabling the instances ``@1`` ..
``@N``.  Units run as the site's own unprivileged user.  Everything is derived
from the ``SiteWorker`` rows, so ``lite-panel rebuild`` recreates them.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from sqlalchemy.orm import Session as OrmSession

from app.models import Site, SiteWorker
from app.providers import get_provider
from app.services import apps as apps_service
from app.services import logs as logs_service
from app.services import renderer, system
from app.shell import which
from app.validators import (
    MAX_WORKER_PROCESSES,
    ValidationError,
    validate_stop_timeout,
    validate_worker_command,
    validate_worker_name,
    validate_worker_processes,
)

logger = logging.getLogger(__name__)

UNIT_DIR = apps_service.UNIT_DIR
UNIT_PREFIX = "lite-panel-worker-"
MAX_WORKERS_PER_SITE = 10
DEFAULT_STOP_TIMEOUT = 60

_UNIT_NAME = re.compile(r"^lite-panel-worker-[a-z0-9_-]+\.[a-z0-9-]+@(\d+|\*)\.service$")

PHP_PRESETS: List[Dict] = [
    {
        "label": "Laravel queue worker",
        "name": "queue",
        "command": "{PHP} artisan queue:work --sleep=3 --tries=3 --max-time=3600",
        "processes": 1,
    },
    {"label": "Laravel Horizon", "name": "horizon", "command": "{PHP} artisan horizon", "processes": 1},
    {"label": "Laravel Reverb", "name": "reverb", "command": "{PHP} artisan reverb:start", "processes": 1},
    {
        "label": "Laravel scheduler (instead of cron)",
        "name": "schedule",
        "command": "{PHP} artisan schedule:work",
        "processes": 1,
    },
]
NODE_PRESETS: List[Dict] = [
    {"label": "node worker.js", "name": "worker", "command": "node worker.js", "processes": 1},
    {"label": "npm run worker", "name": "worker", "command": "npm run worker", "processes": 1},
]
PYTHON_PRESETS: List[Dict] = [
    {"label": "Celery worker", "name": "celery", "command": "celery -A app worker -l info", "processes": 1},
    {"label": "RQ worker", "name": "rq", "command": "rq worker", "processes": 1},
    {"label": "python worker.py", "name": "worker", "command": "python worker.py", "processes": 1},
]


def presets_for(site) -> List[Dict]:
    if site.app_type == "node":
        return NODE_PRESETS
    if site.app_type == "python":
        return PYTHON_PRESETS
    return PHP_PRESETS


# --------------------------------------------------------------------------
# Identity and paths
# --------------------------------------------------------------------------


def base_name(site_name: str, worker_name: str) -> str:
    # "." cannot occur in either part, so ("a-b", "c") and ("a", "b-c") differ.
    return f"{UNIT_PREFIX}{site_name}.{worker_name}"


def template_name(site_name: str, worker_name: str) -> str:
    return f"{base_name(site_name, worker_name)}@.service"


def instance_name(site_name: str, worker_name: str, index) -> str:
    return f"{base_name(site_name, worker_name)}@{index}.service"


def instance_names(site_name: str, worker_name: str, count: int) -> List[str]:
    return [instance_name(site_name, worker_name, i) for i in range(1, count + 1)]


def is_worker_unit(name: str) -> bool:
    return bool(_UNIT_NAME.match(name or ""))


def unit_path(site_name: str, worker_name: str) -> Path:
    return UNIT_DIR / template_name(site_name, worker_name)


def workdir(site) -> Path:
    """Where the command runs.  For a PHP site whose document path is the
    framework's ``public`` folder (Laravel), that's the project root above it,
    where ``artisan`` lives."""
    base = Path(site.webroot)
    if not apps_service.is_app(site) and base.name == "public":
        return base.parent
    return base


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def uses_php(command: str) -> bool:
    return "{PHP}" in command


def resolved_command(site, worker) -> str:
    command = worker.command
    if uses_php(command):
        if not site.php_version:
            raise ValidationError("This command uses {PHP}, but the site has no PHP version.")
        command = command.replace("{PHP}", get_provider("php").cli_path_for(site.php_version))
    return command


def path_env(site) -> str:
    if apps_service.is_app(site):
        return apps_service.path_env(site)
    return "/usr/local/bin:/usr/bin:/bin"


def render_unit(site, worker) -> str:
    return renderer.render(
        "worker.service.j2",
        {
            "site": site,
            "worker": worker,
            "workdir": str(workdir(site)),
            "path_env": path_env(site),
            "env_file": str(apps_service.env_path(site.name)) if apps_service.is_app(site) else None,
            "exec_start": apps_service.systemd_quote(resolved_command(site, worker)),
        },
    )


def render_files(site) -> bool:
    """Write every worker's unit; reload systemd if any changed.  Returns
    True when something changed (running workers need a restart)."""
    changed = False
    for worker in site.workers:
        try:
            text = render_unit(site, worker)
        except ValidationError as exc:
            logger.warning("worker %s of %s not rendered: %s", worker.name, site.name, exc)
            continue
        changed |= renderer.render_to_file_text(unit_path(site.name, worker.name), text)
    if changed:
        apps_service._systemctl(["daemon-reload"])
    return changed


# --------------------------------------------------------------------------
# State and logs
# --------------------------------------------------------------------------


@dataclass
class WorkerState:
    running: int
    total: int

    @property
    def active(self) -> bool:
        return self.total > 0 and self.running == self.total

    @property
    def label(self) -> str:
        if self.running == 0:
            return "stopped"
        if self.running == self.total:
            return "running"
        return f"{self.running}/{self.total} running"


def state(site, worker) -> WorkerState:
    units = instance_names(site.name, worker.name, worker.processes)
    running = sum(1 for unit in units if system.service_state(unit).active)
    return WorkerState(running=running, total=len(units))


def recent_logs(site, worker, lines: int = 100) -> str:
    return logs_service.tail_journal(instance_name(site.name, worker.name, "*"), lines)


# --------------------------------------------------------------------------
# Control
# --------------------------------------------------------------------------


def _settle_and_verify(ctx, site, worker, wait: float) -> None:
    time.sleep(wait)
    current = state(site, worker)
    if current.active:
        ctx.log(f"Worker {worker.name}: {current.total} process(es) running")
        return
    ctx.log(f"Worker {worker.name} did not stay running ({current.label}). Recent output:")
    ctx.log(recent_logs(site, worker, 25))
    raise RuntimeError(
        f"Worker {worker.name} exited right after starting. Check the command and "
        "that the site's dependencies are installed, then try again."
    )


def control(ctx, site, worker, action: str, *, wait: float = 2.0) -> None:
    if action not in ("start", "stop", "restart"):
        raise ValidationError("Unknown action.")
    units = instance_names(site.name, worker.name, worker.processes)
    if not unit_path(site.name, worker.name).exists():
        render_files(site)

    if action == "stop":
        ctx.check(["systemctl", "disable", "--now", *units], timeout=120)
        ctx.log(f"Worker {worker.name} stopped")
        return

    if action == "start":
        ctx.check(["systemctl", "enable", "--now", *units], timeout=120)
    else:
        ctx.check(["systemctl", "enable", *units], timeout=60)
        ctx.check(["systemctl", "restart", *units], timeout=120)
    _settle_and_verify(ctx, site, worker, wait)


def control_all(ctx, site, action: str, *, wait: float = 2.0) -> None:
    if not site.workers:
        raise ValidationError("This site has no workers.")
    failures = []
    for worker in site.workers:
        try:
            control(ctx, site, worker, action, wait=wait)
        except RuntimeError as exc:
            failures.append(str(exc))
    if failures:
        raise RuntimeError(" ".join(failures))


def restart_php_workers(ctx, site) -> None:
    """After a PHP version change: restart the workers that use ``{PHP}``."""
    for worker in site.workers:
        if uses_php(worker.command) and state(site, worker).running:
            ctx.run(["systemctl", "restart", *instance_names(site.name, worker.name, worker.processes)])
            ctx.log(f"Restarted worker {worker.name} on the new PHP version")


def _stop_all_instances(ctx, site_name: str, worker_name: str) -> None:
    if which("systemctl"):
        ctx.run(
            [
                "systemctl", "disable", "--now",
                *instance_names(site_name, worker_name, MAX_WORKER_PROCESSES),
            ],
            timeout=120,
        )


# --------------------------------------------------------------------------
# Changes
# --------------------------------------------------------------------------


def _clean_fields(site, name, command, processes, stop_timeout):
    name = validate_worker_name(name) if name is not None else None
    command = validate_worker_command(command)
    processes = validate_worker_processes(processes)
    stop_timeout = validate_stop_timeout(stop_timeout)
    if uses_php(command) and not site.php_version:
        raise ValidationError("This command uses {PHP}, but the site has no PHP version.")
    return name, command, processes, stop_timeout


def validate_new(site, name, command, processes, stop_timeout):
    """Field validation shared by the route (fast feedback) and the job."""
    name, command, processes, stop_timeout = _clean_fields(
        site, name, command, processes, stop_timeout
    )
    if any(w.name == name for w in site.workers):
        raise ValidationError(f"This site already has a worker named {name}.")
    if len(site.workers) >= MAX_WORKERS_PER_SITE:
        raise ValidationError(f"A site can have at most {MAX_WORKERS_PER_SITE} workers.")
    return name, command, processes, stop_timeout


def add_worker(db: OrmSession, ctx, site: Site, *, name, command, processes, stop_timeout) -> SiteWorker:
    name, command, processes, stop_timeout = validate_new(site, name, command, processes, stop_timeout)
    worker = SiteWorker(
        site_id=site.id, name=name, command=command, processes=processes, stop_timeout=stop_timeout
    )
    db.add(worker)
    db.flush()
    db.refresh(site)
    render_files(site)
    db.commit()
    ctx.log(f"Worker {name} added")
    control(ctx, site, worker, "start")
    return worker


def update_worker(
    db: OrmSession, ctx, site: Site, worker: SiteWorker, *, command, processes, stop_timeout
) -> None:
    _, command, processes, stop_timeout = _clean_fields(site, None, command, processes, stop_timeout)
    was_running = state(site, worker).running > 0
    previous = worker.processes

    worker.command = command
    worker.processes = processes
    worker.stop_timeout = stop_timeout
    db.flush()
    render_files(site)
    db.commit()
    ctx.log(f"Worker {worker.name} saved")

    if processes < previous:
        dropped = [
            instance_name(site.name, worker.name, i) for i in range(processes + 1, previous + 1)
        ]
        ctx.run(["systemctl", "disable", "--now", *dropped], timeout=120)
    if was_running:
        control(ctx, site, worker, "restart")
    else:
        ctx.log("The worker isn't running; start it to apply the changes.")


def delete_worker(db: OrmSession, ctx, site: Site, worker: SiteWorker) -> None:
    name = worker.name
    _stop_all_instances(ctx, site.name, name)
    renderer.remove_file(unit_path(site.name, name))
    db.delete(worker)
    db.commit()
    apps_service._systemctl(["daemon-reload"])
    ctx.log(f"Worker {name} removed")


def teardown(ctx, site) -> None:
    """Stop and forget every worker of a site (site deletion).  The rows go
    with the site; this removes what they put on disk and in systemd."""
    for worker in list(site.workers):
        _stop_all_instances(ctx, site.name, worker.name)
        renderer.remove_file(unit_path(site.name, worker.name))
    if site.workers:
        apps_service._systemctl(["daemon-reload"])
        ctx.log(f"removed {len(site.workers)} worker service(s)")
