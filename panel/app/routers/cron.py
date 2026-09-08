"""Cron jobs -- per-site, or server-wide (root) when a job isn't any one
site's business, or there's no site yet.

Every job is installed into a real crontab: a site's own system user's, or
root's for a server-wide job -- see app.services.cron for why a site-scoped
job never touches root. The database row is the source of truth and is
written immediately; actually installing it (running ``crontab``) goes
through the same job queue as every other privileged operation in this
panel (see app.jobs), because it touches the system outside the
request/response cycle.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.database import get_session
from app.deps import csrf_protect, render, require_session
from app.jobs import enqueue
from app.models import AuditLog, CronJob, Site
from app.providers import get_provider
from app.services import cron as cron_service
from app.validators import ValidationError, validate_cron_command, validate_cron_field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cron")

# The site dropdown's sentinel value for "no site -- runs as root". Never a
# valid Site.id (those are positive integers from an autoincrement PK), so
# it can't collide with a real site.
SERVER_SCOPE = "server"


def _php_cli_paths() -> list:
    """The real, invocable path for every PHP version installed on this
    server -- shown next to the command field so a job doesn't have to guess
    whether "php" on its own resolves to anything."""
    php = get_provider("php")
    return [php.cli_path_for(v) for v in php.installed_versions()]


def _resolve_site(db: OrmSession, site_id: str) -> tuple:
    """Turn the submitted site_id (a real id, or SERVER_SCOPE) into a Site
    or None. Returns (site, error) -- exactly one is set."""
    if site_id == SERVER_SCOPE:
        return None, None
    try:
        site_pk = int(site_id)
    except (TypeError, ValueError):
        return None, "Choose a site or Server (root)."
    site = db.get(Site, site_pk)
    if site is None:
        return None, "That site no longer exists."
    return site, None


@router.get("")
def cron_list(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    jobs = db.scalars(
        select(CronJob)
        .outerjoin(Site)
        .order_by(CronJob.site_id.is_(None).desc(), Site.domain, CronJob.id)
    ).all()
    sites = db.scalars(select(Site).where(Site.is_active).order_by(Site.domain)).all()

    return render(
        request,
        "cron/list.html",
        session=session,
        user=session.user,
        jobs=jobs,
        sites=sites,
        server_scope=SERVER_SCOPE,
        cron_available=cron_service.is_available(),
        presets=cron_service.PRESETS,
        php_cli_paths=_php_cli_paths(),
    )


@router.post("/new", dependencies=[Depends(csrf_protect)])
def create(
    site_id: str = Form(...),
    description: str = Form(""),
    minute: str = Form(...),
    hour: str = Form(...),
    day_of_month: str = Form(...),
    month: str = Form(...),
    day_of_week: str = Form(...),
    command: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site, error = _resolve_site(db, site_id)
    if error:
        return _back(error=error)

    try:
        job = CronJob(
            site_id=site.id if site else None,
            description=description.strip()[:255],
            minute=validate_cron_field(minute, field="minute"),
            hour=validate_cron_field(hour, field="hour"),
            day_of_month=validate_cron_field(day_of_month, field="day of month"),
            month=validate_cron_field(month, field="month"),
            day_of_week=validate_cron_field(day_of_week, field="day of week"),
            command=validate_cron_command(command),
        )
    except ValidationError as exc:
        return _back(error=str(exc))

    db.add(job)
    db.commit()
    _audit(db, session, "cron.create", f"{job.target_label}: {job.command[:100]}")

    sync_job = _sync(db, session, site)
    return RedirectResponse(f"/jobs/{sync_job.id}", status_code=303)


@router.get("/{job_id}/edit")
def edit_form(
    job_id: int,
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    job = db.get(CronJob, job_id)
    if job is None:
        return _back(error="That cron job no longer exists.")

    return render(
        request,
        "cron/edit.html",
        session=session,
        user=session.user,
        job=job,
        presets=cron_service.PRESETS,
        selected_schedule=cron_service.match_preset(job.schedule_display),
        php_cli_paths=_php_cli_paths(),
    )


@router.post("/{job_id}/edit", dependencies=[Depends(csrf_protect)])
def edit(
    job_id: int,
    description: str = Form(""),
    minute: str = Form(...),
    hour: str = Form(...),
    day_of_month: str = Form(...),
    month: str = Form(...),
    day_of_week: str = Form(...),
    command: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    job = db.get(CronJob, job_id)
    if job is None:
        return _back(error="That cron job no longer exists.")

    try:
        job.description = description.strip()[:255]
        job.minute = validate_cron_field(minute, field="minute")
        job.hour = validate_cron_field(hour, field="hour")
        job.day_of_month = validate_cron_field(day_of_month, field="day of month")
        job.month = validate_cron_field(month, field="month")
        job.day_of_week = validate_cron_field(day_of_week, field="day of week")
        job.command = validate_cron_command(command)
    except ValidationError as exc:
        db.rollback()
        return RedirectResponse(
            f"/cron/{job_id}/edit?error={quote(str(exc))}", status_code=303
        )

    db.commit()
    _audit(db, session, "cron.edit", f"{job.target_label}: {job.command[:100]}")

    sync_job = _sync(db, session, job.site)
    return RedirectResponse(f"/jobs/{sync_job.id}", status_code=303)


@router.post("/{job_id}/toggle", dependencies=[Depends(csrf_protect)])
def toggle(
    job_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    job = db.get(CronJob, job_id)
    if job is None:
        return _back(error="That cron job no longer exists.")

    job.is_enabled = not job.is_enabled
    db.commit()
    _audit(db, session, "cron.toggle", f"{job.target_label}: {'enabled' if job.is_enabled else 'disabled'}")

    sync_job = _sync(db, session, job.site)
    return RedirectResponse(f"/jobs/{sync_job.id}", status_code=303)


@router.post("/{job_id}/delete", dependencies=[Depends(csrf_protect)])
def delete(
    job_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    job = db.get(CronJob, job_id)
    if job is None:
        return _back(error="That cron job no longer exists.")

    site = job.site
    target_label = job.target_label
    command = job.command
    db.delete(job)
    db.commit()
    _audit(db, session, "cron.delete", f"{target_label}: {command[:100]}")

    sync_job = _sync(db, session, site)
    return RedirectResponse(f"/jobs/{sync_job.id}", status_code=303)


# --------------------------------------------------------------------------


def _sync(db: OrmSession, session, site: Optional[Site]):
    """Enqueue the job that actually installs the crontab from what is now
    in the database -- the same async pattern every other privileged,
    system-touching action in this panel uses (see app.jobs)."""
    label = site.domain if site else "the server"
    return enqueue(
        db,
        "cron.sync",
        f"Update cron jobs for {label}",
        payload={"site_id": site.id if site else None},
        user_id=session.user_id,
    )


def _back(*, notice: Optional[str] = None, error: Optional[str] = None):
    params = []
    if notice:
        params.append(f"notice={quote(notice)}")
    if error:
        params.append(f"error={quote(error)}")
    suffix = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(f"/cron{suffix}", status_code=303)


def _audit(db: OrmSession, session, action: str, target: Optional[str]) -> None:
    db.add(
        AuditLog(
            user_id=session.user_id,
            username=session.user.username,
            action=action,
            target=(target or "")[:255],
        )
    )
    db.commit()
