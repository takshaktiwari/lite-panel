"""FTP accounts, one per site."""

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
from app.models import AuditLog, FtpAccount, Site
from app.providers import get_provider
from app.providers.vsftpd import PASSIVE_MAX_PORT, PASSIVE_MIN_PORT
from app.security import generate_password
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
    linked = {account.site_id for account in accounts}

    return render(
        request,
        "ftp/list.html",
        session=session,
        user=session.user,
        accounts=accounts,
        vsftpd_ready=get_provider("vsftpd").is_installed(),
        available_sites=db.scalars(
            select(Site).where(Site.id.notin_(linked) if linked else True).order_by(Site.name)
        ).all(),
        suggested_password=generate_password(),
        public_ip=system.public_ip() or "your server's IP",
        passive_range=f"{PASSIVE_MIN_PORT}-{PASSIVE_MAX_PORT}",
    )


@router.post("/enable", dependencies=[Depends(csrf_protect)])
def enable(
    site_id: int = Form(...),
    password: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    if not get_provider("vsftpd").is_installed():
        return _back(error="vsftpd is not installed. Install it from the Stack page.")

    site = db.get(Site, site_id)
    if site is None:
        return _back(error="That site no longer exists.")
    if not password:
        return _back(error="A password is required.")

    job = enqueue(
        db,
        "ftp.enable",
        f"Enable FTP for {site.domain}",
        payload={"site_id": site.id, "password": password},
        user_id=session.user_id,
    )
    _audit(db, session, "ftp.enable", site.domain)

    return RedirectResponse(
        f"/jobs/{job.id}?notice="
        + quote(
            f"FTP user {site.system_user} — save this password now, it is not stored: {password}"
        ),
        status_code=303,
    )


@router.post("/{account_id}/password", dependencies=[Depends(csrf_protect)])
def change_password(
    account_id: int,
    password: str = Form(...),
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
    return RedirectResponse(
        f"/jobs/{job.id}?notice={quote(f'New password (not stored): {password}')}",
        status_code=303,
    )


@router.post("/{account_id}/disable", dependencies=[Depends(csrf_protect)])
def disable(
    account_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    account = db.get(FtpAccount, account_id)
    if account is None:
        return _back(error="That FTP account no longer exists.")

    job = enqueue(
        db,
        "ftp.disable",
        f"Disable FTP for {account.username}",
        payload={"account_id": account.id},
        user_id=session.user_id,
    )
    _audit(db, session, "ftp.disable", account.username)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


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
