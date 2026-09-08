"""Databases: create, delete, reset password.

Browsing rows and running queries is Adminer's job, linked from here rather
than reimplemented -- that delegation is the point of the design.
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
from app.models import AuditLog, Site, SiteDatabase
from app.providers import get_provider
from app.security import generate_password
from app.services import databases as db_service
from app.validators import ValidationError, validate_db_identifier

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/databases")


@router.get("")
def database_list(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    mariadb_ready = get_provider("mariadb").is_installed()

    records = db.scalars(select(SiteDatabase).order_by(SiteDatabase.db_name)).all()
    sizes = {}
    unmanaged = []
    existing_users = set()

    # Collect known users from panel DB
    for record in records:
        if record.db_user:
            existing_users.add(record.db_user)

    if mariadb_ready and db_service.is_available():
        known = {record.db_name for record in records}
        for entry in db_service.list_databases():
            sizes[entry["name"]] = entry["size_mb"]
            if entry["name"] not in known:
                unmanaged.append(entry)

        for u in db_service.list_database_users():
            existing_users.add(u)

    return render(
        request,
        "databases/list.html",
        session=session,
        user=session.user,
        databases=records,
        sizes=sizes,
        unmanaged=unmanaged,
        existing_users=sorted(existing_users),
        mariadb_ready=mariadb_ready,
        sites=db.scalars(select(Site).order_by(Site.name)).all(),
        suggested_password=generate_password(),
    )


@router.post("/new", dependencies=[Depends(csrf_protect)])
def create_database(
    request: Request,
    db_name: str = Form(...),
    user_mode: str = Form("new"),
    existing_user: str = Form(""),
    db_user: str = Form(""),
    password: str = Form(""),
    site_id: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    if not get_provider("mariadb").is_installed():
        return _back(error="MariaDB is not installed. Install it from the Stack page.")

    try:
        db_name = validate_db_identifier(db_name)
        if user_mode == "existing" and existing_user.strip():
            chosen_user = validate_db_identifier(existing_user.strip(), kind="user")
        else:
            chosen_user = validate_db_identifier(db_user.strip() or db_name, kind="user")
    except ValidationError as exc:
        return _back(error=str(exc))

    # If it's a new user, password is required.
    # If it's an existing user, password is not accepted or changed (user keeps existing password).
    if user_mode == "existing":
        password = None
    else:
        password = password.strip()
        if not password:
            return _back(error="A password is required for a new user.")

    if db.scalar(select(SiteDatabase).where(SiteDatabase.db_name == db_name)):
        return _back(error=f"Database '{db_name}' is already managed by the panel.")

    job = enqueue(
        db,
        "database.create",
        f"Create database {db_name}",
        payload={
            "db_name": db_name,
            "db_user": chosen_user,
            "password": password,
            "site_id": int(site_id) if site_id.strip().isdigit() else None,
        },
        user_id=session.user_id,
    )
    _audit(db, session, "database.create", db_name)

    notice_msg = f"Database created with existing user '{chosen_user}'."
    if password:
        notice_msg = f"Save this password now — it is not stored: {password}"

    return job_redirect(job.id, "/databases", notice=notice_msg)


@router.post("/user/password", dependencies=[Depends(csrf_protect)])
def reset_user_password(
    db_user: str = Form(...),
    password: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        db_user = validate_db_identifier(db_user.strip(), kind="user")
    except ValidationError as exc:
        return _back(error=str(exc))

    password = password.strip()
    if not password:
        return _back(error="A password is required.")

    job = enqueue(
        db,
        "database.password",
        f"Reset password for {db_user}",
        payload={"db_user": db_user, "password": password},
        user_id=session.user_id,
    )
    _audit(db, session, "database.password", db_user)
    return job_redirect(
        job.id, "/databases", notice=f"New password for {db_user} (not stored): {password}"
    )


@router.post("/{database_id}/delete", dependencies=[Depends(csrf_protect)])
def delete_database(
    database_id: int,
    confirm: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    record = db.get(SiteDatabase, database_id)
    if record is None:
        return _back(error="That database is no longer managed by the panel.")

    if confirm.strip() != record.db_name:
        return _back(error="Type the database name exactly to confirm deletion.")

    job = enqueue(
        db,
        "database.delete",
        f"Delete database {record.db_name}",
        payload={"database_id": record.id},
        user_id=session.user_id,
    )
    _audit(db, session, "database.delete", record.db_name)
    return job_redirect(job.id, "/databases")


@router.post("/{database_id}/user", dependencies=[Depends(csrf_protect)])
def reassign_user(
    database_id: int,
    user_mode: str = Form("existing"),
    existing_user: str = Form(""),
    db_user: str = Form(""),
    password: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    record = db.get(SiteDatabase, database_id)
    if record is None:
        return _back(error="That database is no longer managed by the panel.")

    try:
        if user_mode == "existing" and existing_user.strip():
            chosen_user = validate_db_identifier(existing_user.strip(), kind="user")
        else:
            chosen_user = validate_db_identifier(db_user.strip(), kind="user")
    except ValidationError as exc:
        return _back(error=str(exc))

    if user_mode == "existing":
        password = None
    else:
        password = password.strip()
        if not password:
            return _back(error="A password is required for a new user.")

    job = enqueue(
        db,
        "database.update_user",
        f"Reassign user for {record.db_name} to {chosen_user}",
        payload={
            "database_id": record.id,
            "db_user": chosen_user,
            "password": password,
        },
        user_id=session.user_id,
    )
    _audit(db, session, "database.update_user", f"{record.db_name} -> {chosen_user}")
    notice = f"User reassigned to '{chosen_user}'"
    if password:
        notice += f" with new password (not stored): {password}"

    return job_redirect(job.id, "/databases", notice=notice)


@router.post("/{database_id}/password", dependencies=[Depends(csrf_protect)])
def reset_password(
    database_id: int,
    password: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    record = db.get(SiteDatabase, database_id)
    if record is None:
        return _back(error="That database is no longer managed by the panel.")
    password = password.strip()
    if not password:
        return _back(error="A password is required.")

    job = enqueue(
        db,
        "database.password",
        f"Reset password for {record.db_user}",
        payload={"db_user": record.db_user, "password": password},
        user_id=session.user_id,
    )
    _audit(db, session, "database.password", record.db_user)
    return job_redirect(
        job.id,
        "/databases",
        notice=f"New password for {record.db_user} (not stored): {password}",
    )


# --------------------------------------------------------------------------


def _back(*, error: Optional[str] = None):
    suffix = f"?error={quote(error)}" if error else ""
    return RedirectResponse(f"/databases{suffix}", status_code=303)


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
