"""Backup router: create, list, download and delete site backups."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession, selectinload

from app.database import get_session
from app.deps import csrf_protect, job_redirect, render, require_session
from app.jobs import enqueue
from app.models import Site, SiteDatabase
from app.services import backup as backup_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/backup")


@router.get("")
def backup_index(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    sites = db.scalars(select(Site).order_by(Site.name)).all()
    all_backups = backup_service.list_backups()

    # Group backups by site name for easy template access
    backups_by_site: dict[str, list] = {}
    for b in all_backups:
        backups_by_site.setdefault(b["site"], []).append(b)

    return render(
        request,
        "backup/index.html",
        session=session,
        user=session.user,
        sites=sites,
        backups_by_site=backups_by_site,
        all_backups=all_backups,
    )


@router.post("/{site_id}/create", dependencies=[Depends(csrf_protect)])
def create_backup(
    site_id: int,
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return RedirectResponse("/backup", status_code=303)

    job = enqueue(
        db,
        "backup.create",
        f"Backup {site.domain}",
        payload={"site_id": site_id},
        user_id=session.user_id,
    )
    return job_redirect(job.id, "/backup")


@router.get("/download/{site_name}/{filename}")
def download_backup(
    site_name: str,
    filename: str,
    session=Depends(require_session),
):
    try:
        path = backup_service.backup_path(site_name, filename)
    except (FileNotFoundError, ValueError):
        return RedirectResponse("/backup", status_code=303)

    return FileResponse(
        path=str(path),
        media_type="application/gzip",
        filename=filename,
    )


@router.post("/delete/{site_name}/{filename}", dependencies=[Depends(csrf_protect)])
def delete_backup(
    site_name: str,
    filename: str,
    session=Depends(require_session),
):
    try:
        backup_service.delete_backup(site_name, filename)
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("backup delete failed: %s", exc)
    return RedirectResponse("/backup", status_code=303)
