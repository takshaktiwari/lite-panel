"""Config editor: view and save php.ini, nginx.conf, per-site overrides."""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.database import get_session
from app.deps import csrf_protect, render, require_session
from app.models import AuditLog, Site
from app.providers import get_provider
from app.services import config_editor as svc
from app.services.config_editor import SaveError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/config")


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


@router.get("")
def config_index(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    php = get_provider("php")
    php_versions = php.installed_versions()

    nginx_installed = get_provider("nginx").is_installed()

    sites = db.scalars(select(Site).order_by(Site.name)).all()

    return render(
        request,
        "config/index.html",
        session=session,
        user=session.user,
        php_versions=php_versions,
        nginx_installed=nginx_installed,
        sites=sites,
    )


# ---------------------------------------------------------------------------
# Nginx global
# ---------------------------------------------------------------------------


@router.get("/nginx")
def nginx_editor(
    request: Request,
    session=Depends(require_session),
):
    path = svc.NGINX_CONF
    content = svc.read_config(path)
    backups = svc.list_backups(path)

    return render(
        request,
        "config/editor.html",
        session=session,
        user=session.user,
        title="nginx.conf",
        subtitle="Global nginx configuration",
        content=content,
        save_url="/config/nginx",
        back_url="/config",
        mode="nginx",
        backups=[str(b) for b in backups],
        file_path=str(path),
    )


@router.post("/nginx", dependencies=[Depends(csrf_protect)])
def nginx_save(
    request: Request,
    content: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        msg = svc.save_nginx_conf(content)
    except SaveError as exc:
        _audit(db, session, "config.nginx_global", "failed")
        return RedirectResponse(
            f"/config/nginx?error={quote(str(exc))}",
            status_code=303,
        )

    _audit(db, session, "config.nginx_global", "saved")
    return RedirectResponse(
        f"/config/nginx?notice={quote(msg)}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Per-site nginx override
# ---------------------------------------------------------------------------


@router.get("/nginx/site/{site_id}")
def site_override_editor(
    site_id: int,
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _not_found(request, session)

    path = svc.site_override_path(site.name)
    content = svc.read_config(path)
    backups = svc.list_backups(path)

    return render(
        request,
        "config/editor.html",
        session=session,
        user=session.user,
        title=f"{site.domain} — nginx override",
        subtitle=(
            f"Custom nginx directives for {site.domain}. "
            f"Added inside the server block after the panel-generated config."
        ),
        content=content,
        save_url=f"/config/nginx/site/{site_id}",
        back_url="/config",
        mode="nginx",
        backups=[str(b) for b in backups],
        file_path=str(path),
    )


@router.post("/nginx/site/{site_id}", dependencies=[Depends(csrf_protect)])
def site_override_save(
    site_id: int,
    content: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return RedirectResponse("/config?error=Site+not+found", status_code=303)

    try:
        msg = svc.save_site_override(site.name, content)
    except SaveError as exc:
        _audit(db, session, "config.nginx_override", f"{site.domain} failed")
        return RedirectResponse(
            f"/config/nginx/site/{site_id}?error={quote(str(exc))}",
            status_code=303,
        )

    _audit(db, session, "config.nginx_override", f"{site.domain} saved")
    return RedirectResponse(
        f"/config/nginx/site/{site_id}?notice={quote(msg)}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# PHP ini
# ---------------------------------------------------------------------------


@router.get("/php/{version}")
def php_ini_editor(
    version: str,
    request: Request,
    session=Depends(require_session),
):
    from app.validators import ValidationError, validate_php_version

    try:
        version = validate_php_version(version)
    except ValidationError as exc:
        return render(
            request,
            "error.html",
            session=session,
            user=session.user,
            message=str(exc),
            status_code=400,
        )

    php = get_provider("php")
    if not php.is_version_installed(version):
        return render(
            request,
            "error.html",
            session=session,
            user=session.user,
            message=f"PHP {version} is not installed.",
            status_code=404,
        )

    path = svc.php_ini_path(version)
    content = svc.read_config(path)
    backups = svc.list_backups(path)

    return render(
        request,
        "config/editor.html",
        session=session,
        user=session.user,
        title=f"PHP {version} — php.ini",
        subtitle=f"FPM php.ini for PHP {version}",
        content=content,
        save_url=f"/config/php/{version}",
        back_url="/config",
        mode="properties",
        backups=[str(b) for b in backups],
        file_path=str(path),
    )


@router.post("/php/{version}", dependencies=[Depends(csrf_protect)])
def php_ini_save(
    version: str,
    content: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    from app.validators import ValidationError, validate_php_version

    try:
        version = validate_php_version(version)
    except ValidationError as exc:
        return RedirectResponse(f"/config?error={quote(str(exc))}", status_code=303)

    try:
        msg = svc.save_php_ini(version, content)
    except SaveError as exc:
        _audit(db, session, "config.php_ini", f"php{version} failed")
        return RedirectResponse(
            f"/config/php/{version}?error={quote(str(exc))}",
            status_code=303,
        )

    _audit(db, session, "config.php_ini", f"php{version} saved")
    return RedirectResponse(
        f"/config/php/{version}?notice={quote(msg)}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _not_found(request: Request, session):
    return render(
        request,
        "error.html",
        session=session,
        user=session.user,
        message="That site does not exist.",
        status_code=404,
    )


def _audit(db: OrmSession, session, action: str, target: str) -> None:
    db.add(
        AuditLog(
            user_id=session.user_id,
            username=session.user.username,
            action=action,
            target=target,
        )
    )
    db.commit()
