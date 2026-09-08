"""Monitor router: real-time system metrics, top processes, and historical stats."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session as OrmSession

from app.database import get_session
from app.deps import csrf_protect, render, require_session
from app.services import monitor as monitor_service
from app.shell import run

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/monitor")


@router.get("")
def monitor_page(
    request: Request,
    range: str = Query("24h"),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    stats = monitor_service.get_live_metrics()
    processes = monitor_service.get_top_processes(limit=15, sort_by="cpu")

    hours_map = {"1h": 1, "24h": 24, "2d": 48, "7d": 168}
    hours = hours_map.get(range, 24)
    history = monitor_service.get_history(db, hours=hours, max_points=60)

    return render(
        request,
        "monitor/index.html",
        session=session,
        user=session.user,
        stats=stats,
        processes=processes,
        history=history,
        current_range=range,
    )


@router.get("/live")
def live_metrics(
    sort_by: str = Query("cpu"),
    session=Depends(require_session),
):
    """Real-time JSON endpoint for 3s-5s live polling."""
    stats = monitor_service.get_live_metrics()
    processes = monitor_service.get_top_processes(limit=15, sort_by=sort_by)
    return JSONResponse({
        "stats": stats,
        "processes": processes,
    })


@router.get("/history")
def history_metrics(
    range: str = Query("24h"),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    """Historical JSON metrics for chart updating."""
    hours_map = {"1h": 1, "24h": 24, "2d": 48, "7d": 168}
    hours = hours_map.get(range, 24)
    history = monitor_service.get_history(db, hours=hours, max_points=60)
    return JSONResponse({
        "range": range,
        "history": history,
    })


@router.post("/process/{pid}/kill", dependencies=[Depends(csrf_protect)])
def kill_process(
    pid: int,
    session=Depends(require_session),
):
    """Terminate a runaway process by PID."""
    if pid <= 1:
        return JSONResponse({"error": "Cannot kill system init"}, status_code=400)
    try:
        res = run(["kill", "-9", str(pid)], check=False)
        if res.returncode == 0:
            return JSONResponse({"success": True})
        return JSONResponse({"error": res.stderr or "Kill failed"}, status_code=500)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
