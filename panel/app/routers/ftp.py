"""FTP accounts: any number, each scoped to its own folder.

See app.services.ftp and FtpAccount's docstring in app.models for how a
folder ends up owned by a site's own account versus a dedicated one.
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
from app.deps import csrf_protect, job_redirect, render, require_session
from app.jobs import enqueue
from app.models import AuditLog, FtpAccount, Site
from app.providers import get_provider
from app.providers.vsftpd import PASSIVE_MAX_PORT, PASSIVE_MIN_PORT
from app.security import generate_password
from app.services import sites as sites_service
from app.services import system

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ftp")


@router.get("")
def ftp_list(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    accounts = db.scalars(select(FtpAccount).order_by(FtpAccount.username)).all()

    return render(
        request,
        "ftp/list.html",
        session=session,
        user=session.user,
        accounts=accounts,
        vsftpd_ready=get_provider("vsftpd").is_installed(),
        sites=db.scalars(select(Site).order_by(Site.domain)).all(),
        suggested_password=generate_password(),
        public_ip=system.public_ip() or "your server's IP",
        passive_range=f"{PASSIVE_MIN_PORT}-{PASSIVE_MAX_PORT}",
        sites_root=str(sites_service.sites_root()),
    )


@router.post("/create", dependencies=[Depends(csrf_protect)])
def create(
    username: str = Form(""),
    # Form("") rather than Form(...): FastAPI treats a *present but empty*
    # form field as a missing one and 422s before this handler ever runs, so
    # a required field here would make the friendly "X is required" errors
    # below unreachable from a real, clearable HTML input.
    password: str = Form(""),
    path: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    if not get_provider("vsftpd").is_installed():
        return _back(error="vsftpd is not installed. Install it from the Stack page.")
    if not path.strip():
        return _back(error="A path is required.")
    if not password:
        return _back(error="A password is required.")

    job = enqueue(
        db,
        "ftp.create",
        f"Create FTP account for {path}",
        payload={"username": username.strip(), "password": password, "path": path.strip()},
        user_id=session.user_id,
    )
    _audit(db, session, "ftp.create", path)

    return job_redirect(
        job.id, "/ftp", notice=f"Save this password now, it is not stored: {password}"
    )


@router.post("/{account_id}/password", dependencies=[Depends(csrf_protect)])
def change_password(
    account_id: int,
    password: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    account = db.get(FtpAccount, account_id)
    if account is None:
        return _back(error="That FTP account no longer exists.")
    if not password:
        return _back(error="A password is required.")

    job = enqueue(
        db,
        "ftp.password",
        f"Change FTP password for {account.username}",
        payload={"account_id": account.id, "password": password},
        user_id=session.user_id,
    )
    _audit(db, session, "ftp.password", account.username)
    return job_redirect(job.id, "/ftp", notice=f"New password (not stored): {password}")


@router.post("/{account_id}/delete", dependencies=[Depends(csrf_protect)])
def delete(
    account_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    account = db.get(FtpAccount, account_id)
    if account is None:
        return _back(error="That FTP account no longer exists.")

    job = enqueue(
        db,
        "ftp.delete",
        f"Delete FTP account {account.username}",
        payload={"account_id": account.id},
        user_id=session.user_id,
    )
    _audit(db, session, "ftp.delete", account.username)
    return job_redirect(job.id, "/ftp")


# --------------------------------------------------------------------------


def _back(*, error: Optional[str] = None):
    suffix = f"?error={quote(error)}" if error else ""
    return RedirectResponse(f"/ftp{suffix}", status_code=303)


def _audit(db: OrmSession, session, action: str, target: Optional[str]) -> None:
    db.add(
        AuditLog(
            user_id=session.user_id,
            username=session.user.username,
            action=action,
            target=target,
        )
    )
    db.commit()
