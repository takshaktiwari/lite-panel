"""Logs router: interactive inspection of server, service, and site logs."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.database import get_session
from app.deps import render, require_session
from app.models import Site
from app.services import logs as logs_service
from app.validators import ValidationError, validate_site_name

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/logs")


@router.get("")
def logs_page(
    request: Request,
    source: str = Query("service"),  # "service" or "site"
    name: Optional[str] = Query(None),
    log_id: Optional[str] = Query(None),
    lines: int = Query(100),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    sites = db.scalars(select(Site).order_by(Site.name)).all()
    service_targets = logs_service.get_service_log_targets()

    # Determine active selection
    active_target = None
    target_options = []

    if source == "site":
        site_name = name or (sites[0].name if sites else None)
        if site_name:
            try:
                target_options = logs_service.get_site_log_targets(site_name)
            except ValidationError:
                target_options = []
            if log_id:
                active_target = next((t for t in target_options if t.id == log_id), None)
            if not active_target and target_options:
                active_target = target_options[0]
    else:
        source = "service"
        target_options = service_targets
        if log_id:
            active_target = next((t for t in target_options if t.id == log_id), None)
        if not active_target and target_options:
            active_target = target_options[0]

    content = ""
    if active_target:
        if active_target.file_path:
            content = logs_service.tail_file(active_target.file_path, lines=lines)
        elif active_target.service_name:
            content = logs_service.tail_journal(active_target.service_name, lines=lines)

    return render(
        request,
        "logs/view.html",
        session=session,
        user=session.user,
        source=source,
        selected_site=name or (sites[0].name if sites else ""),
        sites=sites,
        target_options=target_options,
        active_target=active_target,
        lines=lines,
        content=content,
    )


@router.get("/content")
def get_log_content(
    source: str = Query("service"),
    name: Optional[str] = Query(None),
    log_id: str = Query(...),
    lines: int = Query(100),
    session=Depends(require_session),
):
    """Async endpoint for polling / refreshing log content without page reload."""
    target = None
    if source == "site" and name:
        try:
            targets = logs_service.get_site_log_targets(name)
            target = next((t for t in targets if t.id == log_id), None)
        except ValidationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    else:
        targets = logs_service.get_service_log_targets()
        target = next((t for t in targets if t.id == log_id), None)

    if not target:
        return JSONResponse({"error": "Log target not found"}, status_code=404)

    if target.file_path:
        content = logs_service.tail_file(target.file_path, lines=lines)
    elif target.service_name:
        content = logs_service.tail_journal(target.service_name, lines=lines)
    else:
        content = "[No log target available]"

    return JSONResponse({"content": content})
