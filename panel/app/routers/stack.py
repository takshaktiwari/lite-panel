"""Installing and removing stack components, and managing PHP extensions."""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as OrmSession

from app.database import get_session
from app.deps import csrf_protect, render, require_session
from app.jobs import enqueue
from app.models import AuditLog
from app.providers import all_providers, get_provider
from app.validators import ValidationError, validate_php_version

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/stack")


@router.get("")
def stack_page(
    request: Request,
    session=Depends(require_session),
):
    php = get_provider("php")
    installed_php = php.installed_versions()

    return render(
        request,
        "stack/index.html",
        session=session,
        user=session.user,
        providers=[p.status() for p in all_providers()],
        php_available=[v for v in php.available_versions() if v not in installed_php],
        php_installed=installed_php,
    )


@router.post("/install", dependencies=[Depends(csrf_protect)])
def install(
    request: Request,
    key: str = Form(...),
    version: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        provider = get_provider(key)
    except KeyError:
        return _back(error=f"Unknown component '{key}'.")

    version = version.strip() or None
    if provider.multi_version and not version:
        return _back(error=f"Choose a {provider.name} version to install.")
    if version:
        try:
            version = validate_php_version(version) if key == "php" else version
        except ValidationError as exc:
            return _back(error=str(exc))

    label = f"Install {provider.name}" + (f" {version}" if version else "")
    job = enqueue(
        db,
        "provider.install",
        label,
        payload={"key": key, "version": version},
        user_id=session.user_id,
    )
    _audit(db, session, "stack.install", f"{key} {version or ''}".strip())
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/uninstall", dependencies=[Depends(csrf_protect)])
def uninstall(
    request: Request,
    key: str = Form(...),
    version: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        provider = get_provider(key)
    except KeyError:
        return _back(error=f"Unknown component '{key}'.")

    version = version.strip() or None
    label = f"Remove {provider.name}" + (f" {version}" if version else "")
    job = enqueue(
        db,
        "provider.uninstall",
        label,
        payload={"key": key, "version": version},
        user_id=session.user_id,
    )
    _audit(db, session, "stack.uninstall", f"{key} {version or ''}".strip())
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


# --------------------------------------------------------------------------
# PHP extensions
# --------------------------------------------------------------------------


@router.get("/php/{version}")
def php_version_page(
    version: str,
    request: Request,
    session=Depends(require_session),
):
    php = get_provider("php")
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

    if not php.is_version_installed(version):
        return render(
            request,
            "error.html",
            session=session,
            user=session.user,
            message=f"PHP {version} is not installed.",
            status_code=404,
        )

    return render(
        request,
        "stack/php.html",
        session=session,
        user=session.user,
        version=version,
        extensions=php.extension_report(version),
        socket=php.socket_for(version),
    )


@router.post("/php/{version}/extensions", dependencies=[Depends(csrf_protect)])
async def update_extensions(
    version: str,
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    php = get_provider("php")
    try:
        version = validate_php_version(version)
    except ValidationError as exc:
        return _back(error=str(exc))

    form = await request.form()
    wanted = set(form.getlist("extensions"))
    current = set(php.installed_extensions(version))

    add: List[str] = sorted(wanted - current)
    remove: List[str] = sorted(current - wanted)

    if not add and not remove:
        return RedirectResponse(
            f"/stack/php/{version}?notice=No+changes+to+apply", status_code=303
        )

    job = enqueue(
        db,
        "php.extensions",
        f"Update PHP {version} extensions",
        payload={"version": version, "add": add, "remove": remove},
        user_id=session.user_id,
    )
    _audit(db, session, "stack.extensions", f"php{version}: +{len(add)} -{len(remove)}")
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


# --------------------------------------------------------------------------
# Maintenance
# --------------------------------------------------------------------------


@router.post("/rebuild", dependencies=[Depends(csrf_protect)])
def rebuild(
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    job = enqueue(
        db,
        "panel.rebuild",
        "Rebuild all configuration",
        user_id=session.user_id,
    )
    _audit(db, session, "stack.rebuild", None)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


# --------------------------------------------------------------------------


def _back(*, error: Optional[str] = None, notice: Optional[str] = None):
    from urllib.parse import quote

    if error:
        return RedirectResponse(f"/stack?error={quote(error)}", status_code=303)
    return RedirectResponse(f"/stack?notice={quote(notice or '')}", status_code=303)


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
