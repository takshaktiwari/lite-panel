"""First-run setup wizard.

This is where the server actually gets provisioned.  It is deliberately *not*
in install.sh: doing it here means the choices are made against what this
machine can really offer (discovered from apt), can be watched as they run,
and can be changed afterwards from the Stack page -- none of which is true of
answers typed into a shell script once.
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.database import get_session
from app.deps import csrf_protect, job_redirect, render, require_session
from app.jobs import enqueue
from app.models import AuditLog, InstalledProvider
from app.providers import get_provider
from app.services import system, tuning
from app.validators import ValidationError, validate_php_version

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/setup")


def is_complete(db: OrmSession) -> bool:
    """Setup counts as done once anything has been provisioned through it."""
    return bool(db.scalar(select(func.count(InstalledProvider.id))))


@router.get("")
def wizard(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    php = get_provider("php")
    available = php.available_versions()

    return render(
        request,
        "setup/wizard.html",
        session=session,
        user=session.user,
        supported_os=system.is_supported_os(),
        os_name=system.os_release_name(),
        php_available=available,
        php_installed=php.installed_versions(),
        # Newest version pre-selected, but every one on offer comes from apt.
        suggested_php=available[-1] if available else None,
        tuning=tuning.summary(),
        already_done=is_complete(db),
    )


@router.post("/run", dependencies=[Depends(csrf_protect)])
async def run_setup(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    if not system.is_supported_os():
        return RedirectResponse(
            "/setup?error=This+machine+is+not+Ubuntu+or+Debian.", status_code=303
        )

    form = await request.form()

    submitted: List[str] = []
    try:
        for value in form.getlist("php_versions"):
            submitted.append(validate_php_version(value))
    except ValidationError as exc:
        return RedirectResponse(f"/setup?error={exc}", status_code=303)

    # Re-check against the live machine rather than trusting the checkboxes
    # the page was rendered with a moment ago: this is what stands between a
    # stale wizard page and a job that runs apt-get on a version that turns
    # out not to exist here.
    really_available = get_provider("php").available_versions()
    php_versions = [v for v in submitted if v in really_available]
    skipped = [v for v in submitted if v not in really_available]
    if skipped:
        return RedirectResponse(
            "/setup?error="
            + f"PHP {', '.join(skipped)} {'is' if len(skipped) == 1 else 'are'} no longer "
            + "available on this machine — reload the page and pick again.",
            status_code=303,
        )

    payload = {
        "php_versions": php_versions,
        "mariadb": bool(form.get("mariadb")),
        "certbot": bool(form.get("certbot")),
        "vsftpd": bool(form.get("vsftpd")),
    }

    job = enqueue(
        db,
        "setup.bootstrap",
        "Set up the server",
        payload=payload,
        user_id=session.user_id,
    )
    db.add(
        AuditLog(
            user_id=session.user_id,
            username=session.user.username,
            action="setup.run",
            target=", ".join(
                ["nginx"]
                + [f"php{v}" for v in php_versions]
                + [k for k in ("mariadb", "certbot", "vsftpd") if payload[k]]
            ),
        )
    )
    db.commit()

    return job_redirect(job.id, "/setup")


@router.post("/update-check", dependencies=[Depends(csrf_protect)])
def check_updates_now(
    session=Depends(require_session),
):
    from app.services.version import check_for_updates

    info = check_for_updates(force=True)
    if info.update_available:
        notice = f"New version available: {info.latest_version}"
    elif info.error:
        notice = f"Check finished with warning: {info.error}"
    else:
        notice = f"Lite-Panel is up to date (version {info.current_version})."

    return RedirectResponse(f"/setup?notice={notice}#update-section", status_code=303)


@router.post("/update", dependencies=[Depends(csrf_protect)])
async def update_panel_now(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    form = await request.form()
    target_tag = str(form.get("tag") or "").strip() or None

    job = enqueue(
        db,
        "panel.update",
        f"Update Lite-Panel to {target_tag or 'latest'}",
        payload={"tag": target_tag} if target_tag else {},
        user_id=session.user_id,
    )

    db.add(
        AuditLog(
            user_id=session.user_id,
            username=session.user.username,
            action="panel.update",
            target=target_tag or "latest",
        )
    )
    db.commit()

    return job_redirect(job.id, "/setup")

