"""Login and logout."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as OrmSession

from app import security
from app.config import get_settings
from app.database import get_session
from app.deps import client_ip, csrf_protect, optional_session, render
from app.models import AuditLog

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()


@router.get("/login")
def login_form(request: Request, session=Depends(optional_session)):
    if session is not None:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: OrmSession = Depends(get_session),
):
    ip = client_ip(request)

    if security.is_locked_out(db, ip):
        logger.warning("rejected login from locked-out address %s", ip)
        return render(
            request,
            "login.html",
            error="Too many failed attempts. Try again shortly.",
            status_code=429,
        )

    user = security.authenticate(db, username.strip().lower(), password)
    if user is None:
        security.record_failed_login(db, ip)
        db.add(
            AuditLog(action="login.failed", username=username[:64], ip_address=ip)
        )
        db.commit()
        # Deliberately vague: distinguishing "no such user" from "wrong
        # password" would confirm which accounts exist.
        return render(request, "login.html", error="Invalid username or password.")

    security.clear_failed_logins(db, ip)
    session, token = security.create_session(
        db,
        user,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )
    db.add(
        AuditLog(action="login.success", user_id=user.id, username=user.username, ip_address=ip)
    )
    db.commit()

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        security.SESSION_COOKIE,
        token,
        httponly=True,
        # In dev the panel is reached over plain http on localhost; on a real
        # install nginx terminates TLS and the cookie must never travel clear.
        secure=not settings.dev_mode,
        samesite="strict",
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )
    return response


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(
    request: Request,
    session=Depends(optional_session),
    db: OrmSession = Depends(get_session),
):
    if session is not None:
        db.add(
            AuditLog(
                action="logout",
                user_id=session.user_id,
                username=session.user.username,
                ip_address=client_ip(request),
            )
        )
        db.commit()
        security.revoke_session(db, session)

    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(security.SESSION_COOKIE, path="/")
    return response
