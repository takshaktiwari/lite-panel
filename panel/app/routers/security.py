"""Security router: LMD malware scanner and rkhunter rootkit scanner."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session as OrmSession

from app.database import get_session
from app.deps import csrf_protect, job_redirect, render, require_session
from app.jobs import enqueue
from app.services import security as security_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/security")


@router.get("")
def security_page(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    maldet_installed = security_service.is_maldet_installed()
    rkhunter_installed = security_service.is_rkhunter_installed()

    scan_history = []
    quarantine = []
    rkhunter_report = None
    schedules = []

    if maldet_installed:
        try:
            scan_history = security_service.get_maldet_scan_history()
            quarantine = security_service.get_quarantine_list()
        except Exception as exc:
            logger.warning("Could not load maldet history/quarantine: %s", exc)

    if rkhunter_installed:
        try:
            rkhunter_report = security_service.get_rkhunter_last_report()
        except Exception as exc:
            logger.warning("Could not load rkhunter report: %s", exc)

    try:
        schedules = security_service.get_scan_schedules(db)
    except Exception as exc:
        logger.warning("Could not load scan schedules: %s", exc)

    sites = security_service.get_sites()

    return render(
        request,
        "security/index.html",
        session=session,
        user=session.user,
        maldet_installed=maldet_installed,
        rkhunter_installed=rkhunter_installed,
        scan_history=scan_history,
        quarantine=quarantine,
        rkhunter_report=rkhunter_report,
        schedules=schedules,
        sites=sites,
    )


# ---------------------------------------------------------------------------
# Maldet
# ---------------------------------------------------------------------------


@router.post("/maldet/install", dependencies=[Depends(csrf_protect)])
def maldet_install(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    job = enqueue(db, "security.maldet_install", "Install LMD (Malware Detect)",
                  payload={}, user_id=session.user_id)
    return job_redirect(job.id, "/security")


@router.post("/maldet/scan", dependencies=[Depends(csrf_protect)])
def maldet_scan(
    request: Request,
    path: str = Form("/var/www"),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    safe_path = path.strip() or "/var/www"
    job = enqueue(
        db,
        "security.maldet_scan",
        f"Malware scan: {safe_path}",
        payload={"path": safe_path},
        user_id=session.user_id,
    )
    return job_redirect(job.id, "/security")


@router.get("/maldet/report/{scan_id}")
def maldet_report(
    scan_id: str,
    request: Request,
    session=Depends(require_session),
):
    try:
        report = security_service.get_maldet_report(scan_id)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(report)
        raw = report.get("raw") or ""
        if not raw:
            raw = f"SCAN ID: {scan_id}\nTOTAL HITS: {report.get('hits', 0)}\n\n"
            if report.get("hit_list"):
                raw += "FILE HIT LIST:\n"
                for hit in report.get("hit_list", []):
                    raw += f"  [{hit.get('severity', 'high').upper()}] {hit.get('threat')} : {hit.get('path')}\n"
            else:
                raw += "No malware hits detected in this scan.\n"
        return PlainTextResponse(raw)
    except Exception as exc:
        return PlainTextResponse(f"Error reading scan report: {exc}", status_code=500)


@router.post("/maldet/quarantine", dependencies=[Depends(csrf_protect)])
def maldet_quarantine(
    file_path: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    job = enqueue(
        db,
        "security.maldet_quarantine",
        f"Quarantine: {file_path}",
        payload={"file_path": file_path},
        user_id=session.user_id,
    )
    return job_redirect(job.id, "/security")


@router.post("/maldet/delete", dependencies=[Depends(csrf_protect)])
def maldet_delete(
    file_path: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    job = enqueue(
        db,
        "security.maldet_delete",
        f"Delete flagged file: {file_path}",
        payload={"file_path": file_path},
        user_id=session.user_id,
    )
    return job_redirect(job.id, "/security")


@router.post("/maldet/restore", dependencies=[Depends(csrf_protect)])
def maldet_restore(
    filename: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    job = enqueue(
        db,
        "security.maldet_restore",
        f"Restore from quarantine: {filename}",
        payload={"filename": filename},
        user_id=session.user_id,
    )
    return job_redirect(job.id, "/security")


@router.post("/maldet/quarantine-delete", dependencies=[Depends(csrf_protect)])
def maldet_delete_quarantine(
    filename: str = Form(...),
    session=Depends(require_session),
):
    try:
        security_service.delete_quarantine_file(filename)
        return _back(notice=f"Permanently deleted {filename} from quarantine.")
    except Exception as exc:
        return _back(error=str(exc))


# ---------------------------------------------------------------------------
# rkhunter
# ---------------------------------------------------------------------------


@router.post("/rkhunter/install", dependencies=[Depends(csrf_protect)])
def rkhunter_install(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    job = enqueue(db, "security.rkhunter_install", "Install rkhunter",
                  payload={}, user_id=session.user_id)
    return job_redirect(job.id, "/security")


@router.post("/rkhunter/scan", dependencies=[Depends(csrf_protect)])
def rkhunter_scan(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    job = enqueue(db, "security.rkhunter_scan", "rkhunter system scan",
                  payload={}, user_id=session.user_id)
    return job_redirect(job.id, "/security")


# ---------------------------------------------------------------------------
# Scan Schedules
# ---------------------------------------------------------------------------


@router.post("/schedules", dependencies=[Depends(csrf_protect)])
def create_scan_schedule(
    target: str = Form("/var/www"),
    frequency: str = Form("daily"),
    time: Optional[str] = Form(None),
    hour: Optional[int] = Form(None),
    minute: Optional[int] = Form(None),
    day_of_week: int = Form(0),
    day_of_month: int = Form(1),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    clean_target = (target or "").strip()
    if clean_target == "rkhunter":
        scan_type = "rkhunter"
        target_path = "/"
    else:
        scan_type = "maldet"
        target_path = clean_target or "/var/www"

    raw_time = (time or "").strip()
    parsed_hour = hour
    parsed_minute = minute

    if raw_time:
        parts = raw_time.split(":")
        if len(parts) >= 2:
            try:
                parsed_hour = int(parts[0])
                parsed_minute = int(parts[1])
            except ValueError:
                pass
        elif len(parts) == 1:
            try:
                parsed_hour = int(parts[0])
                parsed_minute = 0
            except ValueError:
                pass

    final_hour = 2 if parsed_hour is None else parsed_hour
    final_minute = 0 if parsed_minute is None else parsed_minute

    try:
        sched = security_service.create_scan_schedule(
            db,
            scan_type=scan_type,
            target_path=target_path,
            frequency=frequency,
            hour=final_hour,
            minute=final_minute,
            day_of_week=day_of_week,
            day_of_month=day_of_month,
        )
        desc = security_service.describe_scan_schedule(sched)
        return _back(notice=f"Scan schedule created ({desc}).")
    except Exception as exc:
        return _back(error=f"Failed to create scan schedule: {exc}")


@router.post("/schedules/{schedule_id}/delete", dependencies=[Depends(csrf_protect)])
def delete_scan_schedule(
    schedule_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        security_service.delete_scan_schedule(db, schedule_id)
        return _back(notice="Scan schedule deleted.")
    except Exception as exc:
        return _back(error=str(exc))


@router.post("/schedules/{schedule_id}/toggle", dependencies=[Depends(csrf_protect)])
def toggle_scan_schedule(
    schedule_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        is_active = security_service.toggle_scan_schedule(db, schedule_id)
        status_str = "enabled" if is_active else "paused"
        return _back(notice=f"Scan schedule is now {status_str}.")
    except Exception as exc:
        return _back(error=str(exc))


@router.post("/schedules/{schedule_id}/run", dependencies=[Depends(csrf_protect)])
def run_scan_schedule(
    schedule_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    from app.models import SecurityScanSchedule

    schedule = db.get(SecurityScanSchedule, schedule_id)
    if not schedule:
        return _back(error=f"Scan schedule #{schedule_id} not found.")

    if schedule.scan_type == "rkhunter":
        if not security_service.is_rkhunter_installed():
            return _back(error="rkhunter is not installed on this server.")
        job = enqueue(
            db,
            "security.rkhunter_scan",
            "Run Rootkit Scan (Manual trigger from schedule)",
            payload={},
            user_id=session.user_id,
        )
    else:
        if not security_service.is_maldet_installed():
            return _back(error="Malware Detect (LMD) is not installed on this server.")
        job = enqueue(
            db,
            "security.maldet_scan",
            f"Run Malware Scan: {schedule.target_path}",
            payload={"path": schedule.target_path},
            user_id=session.user_id,
        )

    schedule.last_run_at = datetime.now(timezone.utc)
    db.commit()
    return job_redirect(job.id, "/security")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _back(*, notice: str = "", error: str = "") -> RedirectResponse:
    query = []
    if notice:
        query.append(f"notice={quote(notice)}")
    if error:
        query.append(f"error={quote(error)}")
    suffix = f"?{'&'.join(query)}" if query else ""
    return RedirectResponse(f"/security{suffix}", status_code=303)
