"""Swap: see it, create it, resize it, remove it."""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as OrmSession

from app.database import get_session
from app.deps import csrf_protect, job_redirect, render, require_session
from app.jobs import enqueue
from app.models import AuditLog
from app.services import swap as swap_service
from app.validators import ValidationError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/swap")

# Offered in the size picker; anything from the validator's range can still
# be typed as a custom value.
SIZE_CHOICES_MB = (512, 1024, 2048, 4096, 8192)


@router.get("")
def swap_page(request: Request, session=Depends(require_session)):
    status = swap_service.get_status()
    choices = [size for size in SIZE_CHOICES_MB if size <= swap_service.MAX_SIZE_MB]
    return render(
        request,
        "swap/index.html",
        session=session,
        user=session.user,
        status=status,
        size_choices=choices,
        min_size=swap_service.MIN_SIZE_MB,
        max_size=swap_service.MAX_SIZE_MB,
    )


@router.post("/set", dependencies=[Depends(csrf_protect)])
def set_swap(
    size_mb: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        size = swap_service.validate_size_mb(size_mb)
    except ValidationError as exc:
        return _back(str(exc))
    job = enqueue(
        db, "swap.set", f"Set swap to {size} MB", payload={"size_mb": size}, user_id=session.user_id
    )
    _audit(db, session, "swap.set", f"{size} MB")
    return job_redirect(job.id, "/swap")


@router.post("/remove", dependencies=[Depends(csrf_protect)])
def remove_swap(session=Depends(require_session), db: OrmSession = Depends(get_session)):
    job = enqueue(db, "swap.remove", "Remove swap file", payload={}, user_id=session.user_id)
    _audit(db, session, "swap.remove", None)
    return job_redirect(job.id, "/swap")


@router.post("/swappiness", dependencies=[Depends(csrf_protect)])
def set_swappiness(
    value: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        number = swap_service.validate_swappiness(value)
    except ValidationError as exc:
        return _back(str(exc))
    job = enqueue(
        db, "swap.swappiness", f"Set swappiness to {number}", payload={"value": number},
        user_id=session.user_id,
    )
    _audit(db, session, "swap.swappiness", str(number))
    return job_redirect(job.id, "/swap")


def _back(error: str):
    return RedirectResponse(f"/swap?error={quote(error)}", status_code=303)


def _audit(db: OrmSession, session, action: str, target) -> None:
    db.add(AuditLog(user_id=session.user_id, username=session.user.username, action=action, target=target))
    db.commit()
