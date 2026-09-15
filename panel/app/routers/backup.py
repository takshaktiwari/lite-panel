"""Backup router: create, schedule, list, download and delete site backups."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession, selectinload

from app.database import get_session
from app.deps import csrf_protect, job_redirect, render, require_session
from app.jobs import enqueue
from app.models import BackupSchedule, Site
from app.services import backup as backup_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/backup")


@router.get("")
def backup_index(
    request: Request,
    site: Optional[str] = Query(None),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    sites = db.scalars(select(Site).order_by(Site.domain)).all()

    # Load all backup schedules with attached site
    schedules_raw = db.scalars(
        select(BackupSchedule)
        .options(selectinload(BackupSchedule.site))
        .order_by(BackupSchedule.created_at.desc())
    ).all()

    # Enrich schedules with human-friendly descriptions and next execution times
    schedules = []
    for s in schedules_raw:
        schedules.append({
            "id": s.id,
            "site": s.site,
            "site_id": s.site_id,
            "include_files": s.include_files,
            "include_db": s.include_db,
            "frequency": s.frequency,
            "keep_count": s.keep_count,
            "description": backup_service.describe_schedule(s),
            "next_run": backup_service.get_next_run(s),
            "last_run_at": s.last_run_at,
            "is_enabled": s.is_enabled,
        })

    # Load archives
    all_backups = backup_service.list_backups()

    # Group archives by site name
    backups_by_site: dict[str, list] = {}
    for b in all_backups:
        backups_by_site.setdefault(b["site"], []).append(b)

    # Filtered backups view if user selected a specific site filter
    filtered_backups = [b for b in all_backups if b["site"] == site] if site else all_backups

    return render(
        request,
        "backup/index.html",
        session=session,
        user=session.user,
        sites=sites,
        schedules=schedules,
        all_backups=all_backups,
        filtered_backups=filtered_backups,
        backups_by_site=backups_by_site,
        selected_site=site,
    )


@router.post("/run", dependencies=[Depends(csrf_protect)])
def run_backup_now(
    request: Request,
    site_id: int = Form(...),
    include_files: bool = Form(False),
    include_db: bool = Form(False),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return RedirectResponse("/backup", status_code=303)

    if not include_files and not include_db:
        # Default to both if neither was ticked
        include_files = True
        include_db = True

    scope_desc = "Files + DB" if include_files and include_db else ("Files" if include_files else "DB")
    job = enqueue(
        db,
        "backup.create",
        f"Backup {site.domain} ({scope_desc})",
        payload={
            "site_id": site_id,
            "include_files": include_files,
            "include_db": include_db,
        },
        user_id=session.user_id,
    )
    return job_redirect(job.id, "/backup")


@router.post("/schedule/create", dependencies=[Depends(csrf_protect)])
def create_schedule(
    request: Request,
    site_id: int = Form(...),
    include_files: bool = Form(False),
    include_db: bool = Form(False),
    frequency: str = Form("daily"),
    hour: int = Form(2),
    minute: int = Form(0),
    day_of_week: int = Form(0),
    day_of_month: int = Form(1),
    keep_count: int = Form(7),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return RedirectResponse("/backup", status_code=303)

    if not include_files and not include_db:
        include_files = True
        include_db = True

    hour = max(0, min(23, int(hour)))
    minute = max(0, min(59, int(minute)))
    day_of_week = max(0, min(6, int(day_of_week)))
    day_of_month = max(1, min(31, int(day_of_month)))
    keep_count = max(1, min(100, int(keep_count)))

    valid_frequencies = {"daily", "twice_daily", "weekly", "monthly"}
    if frequency not in valid_frequencies:
        frequency = "daily"

    schedule = BackupSchedule(
        site_id=site_id,
        include_files=include_files,
        include_db=include_db,
        frequency=frequency,
        hour=hour,
        minute=minute,
        day_of_week=day_of_week,
        day_of_month=day_of_month,
        keep_count=keep_count,
        is_enabled=True,
    )
    db.add(schedule)
    db.commit()

    return RedirectResponse("/backup", status_code=303)


@router.post("/schedule/{schedule_id}/toggle", dependencies=[Depends(csrf_protect)])
def toggle_schedule(
    schedule_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    schedule = db.get(BackupSchedule, schedule_id)
    if schedule:
        schedule.is_enabled = not schedule.is_enabled
        db.commit()
    return RedirectResponse("/backup", status_code=303)


@router.post("/schedule/{schedule_id}/run", dependencies=[Depends(csrf_protect)])
def run_schedule_now(
    schedule_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    schedule = db.get(BackupSchedule, schedule_id)
    if schedule is None or not schedule.site:
        return RedirectResponse("/backup", status_code=303)

    scope_desc = "Files + DB" if schedule.include_files and schedule.include_db else (
        "Files" if schedule.include_files else "DB"
    )
    job = enqueue(
        db,
        "backup.create",
        f"Scheduled backup ({scope_desc}) for {schedule.site.domain}",
        payload={
            "site_id": schedule.site_id,
            "include_files": schedule.include_files,
            "include_db": schedule.include_db,
            "schedule_id": schedule.id,
            "keep_count": schedule.keep_count,
        },
        user_id=session.user_id,
    )
    return job_redirect(job.id, "/backup")


@router.post("/schedule/{schedule_id}/delete", dependencies=[Depends(csrf_protect)])
def delete_schedule(
    schedule_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    schedule = db.get(BackupSchedule, schedule_id)
    if schedule:
        db.delete(schedule)
        db.commit()
    return RedirectResponse("/backup", status_code=303)


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
        logger.warning("Backup delete failed: %s", exc)
    return RedirectResponse("/backup", status_code=303)
