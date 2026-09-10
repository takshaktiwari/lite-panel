"""The landing page: what this server is doing right now."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from fastapi.responses import RedirectResponse

from app.database import get_session
from app.deps import render, require_session
from app.models import Job, JobStatus, Site, SiteDatabase
from app.providers import get_provider
from app.services import system

router = APIRouter()


def _services() -> list:
    """Every daemon the panel cares about, in stack order.

    PHP-FPM and Redis come from their providers' own status() rather than
    system.service_states(), since PHP runs one systemd unit per installed
    version (no single "php-fpm" unit to poll) and the provider already
    knows how to aggregate that into "n/m running" -- see PhpProvider.status.
    """
    php_status = get_provider("php").status()
    redis_status = get_provider("redis").status()

    return [
        system.service_state("nginx"),
        system.ServiceState(
            name="PHP-FPM",
            installed=php_status.service_installed,
            active=php_status.service_active,
            detail=php_status.detail,
        ),
        system.service_state("mariadb"),
        system.ServiceState(
            name=redis_status.name,
            installed=redis_status.service_installed,
            active=redis_status.service_active,
            detail=redis_status.detail,
        ),
        system.service_state("vsftpd"),
        system.service_state("lite-panel"),
    ]


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
        services=_services(),
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
