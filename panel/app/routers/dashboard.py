"""The landing page: what this server is doing right now."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from fastapi.responses import RedirectResponse

from app.database import get_session
from app.deps import render, require_session
from app.models import Job, JobStatus, Site, SiteDatabase
from app.services import system

router = APIRouter()


@router.get("/")
def dashboard(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    from app.routers.setup import is_complete

    if not is_complete(db):
        return RedirectResponse("/setup", status_code=303)

    info = system.collect()

    recent_jobs = db.scalars(select(Job).order_by(Job.id.desc()).limit(5)).all()
    running = db.scalar(
        select(func.count(Job.id)).where(Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]))
    )

    return render(
        request,
        "dashboard.html",
        session=session,
        user=session.user,
        info=info,
        uptime=system.format_uptime(info.uptime_seconds),
        server_time=system.server_time(),
        server_ip=system.public_ip(),
        services=system.service_states(),
        site_count=db.scalar(select(func.count(Site.id))) or 0,
        database_count=db.scalar(select(func.count(SiteDatabase.id))) or 0,
        running_jobs=running or 0,
        recent_jobs=recent_jobs,
        supported_os=system.is_supported_os(),
    )


@router.get("/healthz", include_in_schema=False)
def healthz():
    """Liveness probe used by install.sh to confirm the daemon came up."""
    return {"status": "ok"}
