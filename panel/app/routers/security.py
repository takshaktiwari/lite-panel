"""Security router: LMD malware scanner and rkhunter rootkit scanner."""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
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
):
    maldet_installed = security_service.is_maldet_installed()
    rkhunter_installed = security_service.is_rkhunter_installed()

    scan_history = []
    quarantine = []
    rkhunter_report = None

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
    except Exception as exc:
        return render(
            request,
            "security/index.html",
            session=session,
            user=session.user,
            error=str(exc),
        )
    return JSONResponse(report)


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
