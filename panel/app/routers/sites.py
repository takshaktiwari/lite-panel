"""Sites: create, configure, SSL, delete."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession, selectinload

from app.database import get_session
from app.deps import csrf_protect, job_redirect, render, require_session
from app.jobs import enqueue
from app.models import AuditLog, BackupSchedule, CronJob, FtpAccount, Site, SiteDatabase, SiteWorker, SshKey
from app.providers import get_provider
from app.services import apps as apps_service
from app.services import backup as backup_service
from app.services import databases as db_service
from app.services import dns_check as dns_check_service
from app.services import files as files_service
from app.services import sites as sites_service
from app.services import system as system_service
from app.services import workers as workers_service
from app.validators import (
    MAX_WORKER_PROCESSES,
    RUNTIME_APP_TYPES,
    ValidationError,
    validate_app_command,
    validate_app_env,
    validate_app_port,
    validate_app_type,
    validate_domain,
    validate_site_name,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sites")


@router.get("")
def site_list(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    sites = db.scalars(
        select(Site).options(selectinload(Site.databases)).order_by(Site.name)
    ).all()
    return render(
        request,
        "sites/list.html",
        session=session,
        user=session.user,
        sites=sites,
        php_versions=get_provider("php").installed_versions(),
        nginx_ready=get_provider("nginx").is_installed(),
    )


def _runtime_context() -> dict:
    """What the new-site form needs to offer Node.js / Python apps."""
    return {
        "runtimes": {
            key: {
                **info,
                "installed": get_provider(info["provider"]).is_installed(),
                "presets": apps_service.COMMAND_PRESETS[key],
            }
            for key, info in apps_service.RUNTIMES.items()
        },
    }


@router.get("/new")
def new_site_form(
    request: Request,
    session=Depends(require_session),
):
    return render(
        request,
        "sites/new.html",
        session=session,
        user=session.user,
        php_versions=get_provider("php").installed_versions(),
        nginx_ready=get_provider("nginx").is_installed(),
        certbot_ready=get_provider("certbot").is_installed(),
        sites_root=str(sites_service.sites_root()),
        **_runtime_context(),
    )


@router.post("/new", dependencies=[Depends(csrf_protect)])
def create_site(
    request: Request,
    domain: str = Form(...),
    folder: str = Form(""),
    php_version: str = Form(""),
    redirect_www: str = Form(""),
    enable_ssl: str = Form(""),
    app_type: str = Form("web"),
    app_command: str = Form(""),
    app_port: str = Form(""),
    app_env: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    php = get_provider("php")

    try:
        app_type = validate_app_type(app_type)
        if app_type in RUNTIME_APP_TYPES:
            apps_service.require_runtime(app_type)
            app_command = validate_app_command(
                app_command or apps_service.default_command(app_type)
            )
            app_env = validate_app_env(app_env)
            app_port = (
                str(apps_service.check_port_free(db, app_port.strip()))
                if app_port.strip()
                else ""
            )
            php_version = ""
        else:
            app_command, app_port, app_env = "", "", ""
        domain = validate_domain(domain)
        # One field, typed right after the /var/www/ prefix shown in the
        # form: the first segment is the site's own folder (and where its
        # system user's account lives); anything past the first "/" is an
        # optional extra nesting for the webroot (e.g. "blog/public") --
        # most sites don't need it, so it's opt-in rather than a forced
        # default.
        name, subfolder = _split_folder(folder, domain)
    except ValidationError as exc:
        return render(
            request,
            "sites/new.html",
            session=session,
            user=session.user,
            php_versions=php.installed_versions(),
            nginx_ready=get_provider("nginx").is_installed(),
            certbot_ready=get_provider("certbot").is_installed(),
            sites_root=str(sites_service.sites_root()),
            error=str(exc),
            form={
                "domain": domain,
                "folder": folder,
                "app_type": app_type,
                "app_command": app_command,
                "app_port": app_port,
                "app_env": app_env,
            },
            status_code=400,
            **_runtime_context(),
        )

    if db.scalar(select(Site).where(Site.domain == domain)):
        return _back(error=f"{domain} is already served by another site.")
    if db.scalar(select(Site).where(Site.name == name)):
        return _back(error=f"A site named '{name}' already exists.")

    job = enqueue(
        db,
        "site.create",
        f"Create site {domain}",
        payload={
            "domain": domain,
            "name": name,
            "php_version": php_version.strip() or None,
            "subfolder": subfolder,
            "redirect_www": bool(redirect_www),
            "enable_ssl": bool(enable_ssl),
            "app_type": app_type,
            "app_command": app_command or None,
            "app_port": int(app_port) if app_port else None,
            "app_env": app_env,
        },
        user_id=session.user_id,
    )
    _audit(db, session, "site.create", domain)
    return job_redirect(job.id, "/sites")


@router.get("/{site_id}")
def site_detail(
    site_id: int,
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _not_found(request, session)

    certbot = get_provider("certbot")
    site_databases = db.scalars(select(SiteDatabase).where(SiteDatabase.site_id == site.id)).all()

    # Same actions menu as the Databases page (_macros.html's database_table)
    # needs sizes for every database and the full known-user list for its
    # "assign / change user" modal, not just this site's own databases.
    sizes = {}
    existing_users = {
        record.db_user
        for record in db.scalars(select(SiteDatabase)).all()
        if record.db_user
    }
    if get_provider("mariadb").is_installed() and db_service.is_available():
        sizes = db_service.size_map()
        existing_users.update(db_service.list_database_users())

    site_cron_jobs = db.scalars(
        select(CronJob).where(CronJob.site_id == site.id).order_by(CronJob.id.desc())
    ).all()

    schedules_raw = db.scalars(
        select(BackupSchedule)
        .where(BackupSchedule.site_id == site.id)
        .order_by(BackupSchedule.created_at.desc())
    ).all()
    site_schedules = []
    for s in schedules_raw:
        site_schedules.append({
            "id": s.id,
            "include_files": s.include_files,
            "include_db": s.include_db,
            "frequency": s.frequency,
            "keep_count": s.keep_count,
            "description": backup_service.describe_schedule(s),
            "next_run": backup_service.get_next_run(s),
            "last_run_at": s.last_run_at,
            "is_enabled": s.is_enabled,
        })

    site_backups = backup_service.list_backups(site_name=site.name)

    return render(
        request,
        "sites/detail.html",
        session=session,
        user=session.user,
        site=site,
        php_versions=get_provider("php").installed_versions(),
        databases=site_databases,
        sizes=sizes,
        existing_users=sorted(existing_users),
        ftp_account=db.scalar(select(FtpAccount).where(FtpAccount.site_id == site.id)),
        ssh_keys=db.scalars(select(SshKey).where(SshKey.site_id == site.id)).all(),
        cron_jobs=site_cron_jobs,
        backup_schedules=site_schedules,
        backups=site_backups,
        certbot_ready=certbot.is_installed(),
        has_certificate=certbot.has_certificate(site.domain) if certbot.is_installed() else False,
        cert_expiry=certbot.cert_expiry(site.domain) if certbot.is_installed() and site.ssl_enabled else None,
        now=datetime.now(timezone.utc),
        socket=str(sites_service.socket_for(site.name)) if site.php_version else None,
        **_app_context(site),
        **_worker_context(site),
    )


def _worker_context(site: Site) -> dict:
    rows = []
    for worker in site.workers:
        current = workers_service.state(site, worker)
        rows.append({"worker": worker, "state": current})
    return {
        "worker_rows": rows,
        "worker_presets": workers_service.presets_for(site),
        "worker_default_timeout": workers_service.DEFAULT_STOP_TIMEOUT,
        "worker_max_processes": MAX_WORKER_PROCESSES,
        "workers_workdir": str(workers_service.workdir(site)),
        "worker_php_missing": not site.php_version and not apps_service.is_app(site),
    }


def _app_context(site: Site) -> dict:
    if not apps_service.is_app(site):
        return {"is_app": False}
    info = apps_service.RUNTIMES[site.app_type]
    return {
        "is_app": True,
        "app_label": info["label"],
        "app_hint": info["hint"],
        "app_presets": apps_service.COMMAND_PRESETS[site.app_type],
        "app_runtime_ready": get_provider(info["provider"]).is_installed(),
        "app_state": apps_service.state(site),
        "app_unit": apps_service.unit_name(site.name),
        "app_command_default": apps_service.default_command(site.app_type),
    }


@router.get("/{site_id}/dns-check")
def dns_check(
    site_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    """On-demand fan-out to public resolvers, fetched by the detail page's button."""
    site = db.get(Site, site_id)
    if site is None:
        return JSONResponse({"error": "That site no longer exists."}, status_code=404)

    expected_ip = system_service.public_ip()
    results = dns_check_service.check_propagation(site.domain, expected_ip)
    return JSONResponse({
        "domain": site.domain,
        "expected_ip": expected_ip,
        "resolvers": [
            {
                "label": r.label,
                "resolver_ip": r.resolver_ip,
                "region": r.region,
                "answers": r.answers,
                "matches": r.matches,
                "error": r.error,
            }
            for r in results
        ],
    })


@router.get("/{site_id}/disk-usage")
def disk_usage(
    site_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    """On-demand directory walk, fetched by the detail page on load.

    Not part of site_detail's own render -- walking a large site's files can
    take a couple of seconds, and there's no reason the rest of the page
    (which needs none of this) should wait on it.
    """
    site = db.get(Site, site_id)
    if site is None:
        return JSONResponse({"error": "That site no longer exists."}, status_code=404)

    total = files_service.directory_size_bytes(Path(site.root_dir))
    return JSONResponse({"bytes": total, "human": files_service.format_size(total)})


@router.get("/{site_id}/app/status")
def app_status(
    site_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None or not apps_service.is_app(site):
        return JSONResponse({"error": "That application no longer exists."}, status_code=404)
    current = apps_service.state(site)
    return JSONResponse({
        "active": current.active,
        "installed": current.installed,
        "state": current.detail or ("active" if current.active else "inactive"),
    })


@router.get("/{site_id}/app/logs")
def app_logs(
    site_id: int,
    lines: int = 100,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None or not apps_service.is_app(site):
        return JSONResponse({"error": "That application no longer exists."}, status_code=404)
    return JSONResponse({"logs": apps_service.recent_logs(site, lines)})


@router.post("/{site_id}/app/control", dependencies=[Depends(csrf_protect)])
def app_control(
    site_id: int,
    action: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _back(error="That site no longer exists.")
    if not apps_service.is_app(site):
        return _detail_error(site, "This site isn't a Node.js or Python application.")
    if action not in ("start", "stop", "restart"):
        return _detail_error(site, "Unknown action.")

    job = enqueue(
        db,
        "site.app_control",
        f"{action.capitalize()} app for {site.domain}",
        payload={"site_id": site.id, "action": action},
        user_id=session.user_id,
    )
    _audit(db, session, f"site.app_{action}", site.domain)
    return job_redirect(job.id, f"/sites/{site.id}")


@router.post("/{site_id}/app/install", dependencies=[Depends(csrf_protect)])
def app_install(
    site_id: int,
    build: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _back(error="That site no longer exists.")
    if not apps_service.is_app(site):
        return _detail_error(site, "This site isn't a Node.js or Python application.")

    job = enqueue(
        db,
        "site.app_install",
        f"Install dependencies for {site.domain}",
        payload={"site_id": site.id, "build": bool(build)},
        user_id=session.user_id,
    )
    _audit(db, session, "site.app_install", site.domain)
    return job_redirect(job.id, f"/sites/{site.id}")


@router.post("/{site_id}/app/settings", dependencies=[Depends(csrf_protect)])
def app_settings(
    site_id: int,
    command: str = Form(""),
    port: str = Form(""),
    env: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _back(error="That site no longer exists.")
    if not apps_service.is_app(site):
        return _detail_error(site, "This site isn't a Node.js or Python application.")

    # Validate before queueing so a typo is reported on the page, not as a
    # failed job.
    try:
        command = validate_app_command(command)
        port_number = apps_service.check_port_free(db, port, site=site)
        env = validate_app_env(env)
    except ValidationError as exc:
        return _detail_error(site, str(exc))

    job = enqueue(
        db,
        "site.app_settings",
        f"Update app settings for {site.domain}",
        payload={"site_id": site.id, "command": command, "port": port_number, "env": env},
        user_id=session.user_id,
    )
    _audit(db, session, "site.app_settings", site.domain)
    return job_redirect(job.id, f"/sites/{site.id}")


@router.post("/{site_id}/php", dependencies=[Depends(csrf_protect)])
def change_php(
    site_id: int,
    php_version: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _back(error="That site no longer exists.")
    if apps_service.is_app(site):
        return _detail_error(site, "PHP isn't used by Node.js and Python sites.")

    job = enqueue(
        db,
        "site.php_version",
        f"Change {site.domain} to PHP {php_version or 'none'}",
        payload={"site_id": site.id, "php_version": php_version.strip() or None},
        user_id=session.user_id,
    )
    _audit(db, session, "site.php_version", f"{site.domain} -> {php_version or 'none'}")
    return job_redirect(job.id, f"/sites/{site.id}")


@router.post("/{site_id}/webroot", dependencies=[Depends(csrf_protect)])
def change_webroot(
    site_id: int,
    webroot: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _back(error="That site no longer exists.")

    webroot = webroot.strip()
    if not webroot:
        return RedirectResponse(
            f"/sites/{site.id}?error={quote('Document path is required.')}", status_code=303
        )

    job = enqueue(
        db,
        "site.webroot",
        f"Change document path for {site.domain}",
        payload={"site_id": site.id, "webroot": webroot},
        user_id=session.user_id,
    )
    _audit(db, session, "site.webroot", f"{site.domain} -> {webroot}")
    return job_redirect(job.id, f"/sites/{site.id}")


@router.post("/{site_id}/ssl", dependencies=[Depends(csrf_protect)])
def toggle_ssl(
    site_id: int,
    action: str = Form(...),
    email: str = Form(""),
    staging: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _back(error="That site no longer exists.")

    if action == "disable":
        job = enqueue(
            db,
            "site.ssl_disable",
            f"Disable HTTPS for {site.domain}",
            payload={"site_id": site.id},
            user_id=session.user_id,
        )
    else:
        if not get_provider("certbot").is_installed():
            return RedirectResponse(
                f"/sites/{site.id}?error={quote('Install SSL support from the Stack page first.')}",
                status_code=303,
            )
        job = enqueue(
            db,
            "site.ssl_enable",
            f"Enable HTTPS for {site.domain}",
            payload={
                "site_id": site.id,
                "email": email.strip() or None,
                "staging": bool(staging),
            },
            user_id=session.user_id,
        )

    _audit(db, session, f"site.ssl_{action}", site.domain)
    return job_redirect(job.id, f"/sites/{site.id}")


@router.post("/{site_id}/delete", dependencies=[Depends(csrf_protect)])
def delete_site(
    site_id: int,
    confirm: str = Form(""),
    remove_files: str = Form(""),
    delete_databases: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _back(error="That site no longer exists.")

    # Typing the domain is the confirmation: a delete that removes files is
    # not recoverable, and the button alone is too easy to hit.
    if confirm.strip().lower() != site.domain:
        return RedirectResponse(
            f"/sites/{site.id}?error={quote('Type the domain exactly to confirm deletion.')}",
            status_code=303,
        )

    job = enqueue(
        db,
        "site.delete",
        f"Delete site {site.domain}",
        payload={
            "site_id": site.id,
            "remove_files": bool(remove_files),
            "delete_databases": bool(delete_databases),
        },
        user_id=session.user_id,
    )
    _audit(db, session, "site.delete", site.domain)
    return job_redirect(job.id, "/sites")


# --------------------------------------------------------------------------


def _name_from_domain(domain: str) -> str:
    """Derive a slug from a domain: blog.example.com -> blog-example."""
    parts = domain.split(".")
    stem = "-".join(parts[:-1]) if len(parts) > 1 else domain
    cleaned = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in stem.lower())
    return cleaned.strip("-")[:32] or "site"


def _split_folder(folder: str, domain: str) -> tuple:
    """Split what was typed after the /var/www/ prefix into (name, subfolder).

    The first path segment becomes the site's own folder (and the name its
    system user is derived from); anything after the first "/" is an
    optional extra nesting for the webroot, e.g. typing "blog/public" serves
    from /var/www/blog/public while the site itself still lives at
    /var/www/blog. Left blank entirely, the name falls back to one derived
    from the domain and there is no subfolder -- most sites don't need one.
    """
    cleaned = folder.strip().strip("/")
    if not cleaned:
        return validate_site_name(_name_from_domain(domain)), ""

    first, _, rest = cleaned.partition("/")
    name = validate_site_name(first)
    return name, _clean_subfolder(rest)


def _clean_subfolder(value: str) -> str:
    """Validate a webroot path typed relative to a site's home directory.

    Segment-by-segment, like the file manager's containment check, so the
    result can never climb outside root_dir via "..".
    """
    from app.validators import validate_filename

    cleaned = value.strip().strip("/")
    if not cleaned:
        return ""
    segments = [s for s in cleaned.split("/") if s]
    for segment in segments:
        validate_filename(segment)
    return "/".join(segments)


def _detail_error(site: Site, message: str):
    return RedirectResponse(f"/sites/{site.id}?error={quote(message)}", status_code=303)


def _back(*, error: Optional[str] = None):
    suffix = f"?error={quote(error)}" if error else ""
    return RedirectResponse(f"/sites{suffix}", status_code=303)


def _not_found(request: Request, session):
    return render(
        request,
        "error.html",
        session=session,
        user=session.user,
        message="That site does not exist.",
        status_code=404,
    )


def _audit(db: OrmSession, session, action: str, target: Optional[str]) -> None:
    db.add(
        AuditLog(
            user_id=session.user_id,
            username=session.user.username,
            action=action,
            target=target,
        )
    )
    db.commit()


# --------------------------------------------------------------------------
# Background workers (systemd units, see services/workers.py)
# --------------------------------------------------------------------------


def _worker_of(db: OrmSession, site_id: int, worker_id: int):
    site = db.get(Site, site_id)
    worker = db.get(SiteWorker, worker_id)
    if site is None or worker is None or worker.site_id != site.id:
        return site, None
    return site, worker


@router.get("/{site_id}/workers/status")
def workers_status(
    site_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return JSONResponse({"error": "That site no longer exists."}, status_code=404)
    out = []
    for worker in site.workers:
        current = workers_service.state(site, worker)
        out.append({"id": worker.id, "active": current.active, "label": current.label})
    return JSONResponse({"workers": out})


@router.get("/{site_id}/workers/{worker_id}/logs")
def worker_logs(
    site_id: int,
    worker_id: int,
    lines: int = 100,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site, worker = _worker_of(db, site_id, worker_id)
    if worker is None:
        return JSONResponse({"error": "That worker no longer exists."}, status_code=404)
    return JSONResponse({"logs": workers_service.recent_logs(site, worker, lines)})


@router.post("/{site_id}/workers/add", dependencies=[Depends(csrf_protect)])
def worker_add(
    site_id: int,
    name: str = Form(""),
    command: str = Form(""),
    processes: str = Form("1"),
    stop_timeout: str = Form(str(workers_service.DEFAULT_STOP_TIMEOUT)),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _back(error="That site no longer exists.")
    try:
        name, command, count, timeout = workers_service.validate_new(
            site, name, command, processes, stop_timeout
        )
    except ValidationError as exc:
        return _detail_error(site, str(exc))

    job = enqueue(
        db,
        "site.worker_add",
        f"Add worker {name} for {site.domain}",
        payload={
            "site_id": site.id, "name": name, "command": command,
            "processes": count, "stop_timeout": timeout,
        },
        user_id=session.user_id,
    )
    _audit(db, session, "site.worker_add", f"{site.domain}:{name}")
    return job_redirect(job.id, f"/sites/{site.id}")


@router.post("/{site_id}/workers/restart-all", dependencies=[Depends(csrf_protect)])
def workers_restart_all(
    site_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _back(error="That site no longer exists.")
    if not site.workers:
        return _detail_error(site, "This site has no workers.")
    job = enqueue(
        db,
        "site.worker_control",
        f"Restart workers for {site.domain}",
        payload={"site_id": site.id, "action": "restart"},
        user_id=session.user_id,
    )
    _audit(db, session, "site.worker_restart_all", site.domain)
    return job_redirect(job.id, f"/sites/{site.id}")


@router.post("/{site_id}/workers/{worker_id}/control", dependencies=[Depends(csrf_protect)])
def worker_control(
    site_id: int,
    worker_id: int,
    action: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site, worker = _worker_of(db, site_id, worker_id)
    if site is None:
        return _back(error="That site no longer exists.")
    if worker is None:
        return _detail_error(site, "That worker no longer exists.")
    if action not in ("start", "stop", "restart"):
        return _detail_error(site, "Unknown action.")
    job = enqueue(
        db,
        "site.worker_control",
        f"{action.capitalize()} worker {worker.name} for {site.domain}",
        payload={"site_id": site.id, "worker_id": worker.id, "action": action},
        user_id=session.user_id,
    )
    _audit(db, session, f"site.worker_{action}", f"{site.domain}:{worker.name}")
    return job_redirect(job.id, f"/sites/{site.id}")


@router.post("/{site_id}/workers/{worker_id}/settings", dependencies=[Depends(csrf_protect)])
def worker_settings(
    site_id: int,
    worker_id: int,
    command: str = Form(""),
    processes: str = Form("1"),
    stop_timeout: str = Form(str(workers_service.DEFAULT_STOP_TIMEOUT)),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site, worker = _worker_of(db, site_id, worker_id)
    if site is None:
        return _back(error="That site no longer exists.")
    if worker is None:
        return _detail_error(site, "That worker no longer exists.")
    try:
        _, command, count, timeout = workers_service._clean_fields(
            site, None, command, processes, stop_timeout
        )
    except ValidationError as exc:
        return _detail_error(site, str(exc))
    job = enqueue(
        db,
        "site.worker_update",
        f"Update worker {worker.name} for {site.domain}",
        payload={
            "site_id": site.id, "worker_id": worker.id, "command": command,
            "processes": count, "stop_timeout": timeout,
        },
        user_id=session.user_id,
    )
    _audit(db, session, "site.worker_update", f"{site.domain}:{worker.name}")
    return job_redirect(job.id, f"/sites/{site.id}")


@router.post("/{site_id}/workers/{worker_id}/delete", dependencies=[Depends(csrf_protect)])
def worker_delete(
    site_id: int,
    worker_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site, worker = _worker_of(db, site_id, worker_id)
    if site is None:
        return _back(error="That site no longer exists.")
    if worker is None:
        return _detail_error(site, "That worker no longer exists.")
    job = enqueue(
        db,
        "site.worker_delete",
        f"Remove worker {worker.name} from {site.domain}",
        payload={"site_id": site.id, "worker_id": worker.id},
        user_id=session.user_id,
    )
    _audit(db, session, "site.worker_delete", f"{site.domain}:{worker.name}")
    return job_redirect(job.id, f"/sites/{site.id}")
