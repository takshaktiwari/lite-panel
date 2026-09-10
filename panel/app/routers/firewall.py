"""Firewall (UFW) route handlers."""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as OrmSession

from app.database import get_session
from app.deps import csrf_protect, render, require_session
from app.models import AuditLog
from app.services import firewall as fw_service
from app.validators import ValidationError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/firewall")


@router.get("")
def firewall_page(
    request: Request,
    session=Depends(require_session),
):
    status = fw_service.get_status()
    presets = fw_service.ESSENTIAL_PRESETS

    return render(
        request,
        "firewall/index.html",
        session=session,
        user=session.user,
        status=status,
        presets=presets,
    )


@router.post("/toggle", dependencies=[Depends(csrf_protect)])
def toggle_firewall(
    enable: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    should_enable = enable.strip().lower() in ("1", "true", "yes", "on")

    try:
        if should_enable:
            fw_service.enable_firewall()
            action_desc = "firewall.enable"
            msg = "Firewall enabled. Port 22 (SSH) was verified/allowed."
        else:
            fw_service.disable_firewall()
            action_desc = "firewall.disable"
            msg = "Firewall disabled."
    except Exception as exc:
        return _back(error=f"Failed to update firewall state: {exc}")

    _audit(db, session, action_desc, "ufw")
    return _back(notice=msg)


@router.post("/rules/add", dependencies=[Depends(csrf_protect)])
def add_rule(
    port: str = Form(...),
    proto: str = Form("tcp"),
    action: str = Form("allow"),
    from_ip: str = Form("any"),
    comment: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        fw_service.add_rule(
            port=port.strip(),
            proto=proto.strip(),
            action=action.strip(),
            from_ip=from_ip.strip(),
            comment=comment.strip(),
        )
    except (ValidationError, RuntimeError) as exc:
        return _back(error=str(exc))

    target = f"{action.upper()} {port}/{proto} from {from_ip}"
    _audit(db, session, "firewall.rule_add", target)
    return _back(notice=f"Rule added: {target}")


@router.post("/rules/delete", dependencies=[Depends(csrf_protect)])
def delete_rule(
    rule_number: int = Form(...),
    rule_summary: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        fw_service.delete_rule(rule_number)
    except (ValidationError, RuntimeError) as exc:
        return _back(error=str(exc))

    _audit(db, session, "firewall.rule_delete", f"Rule #{rule_number} ({rule_summary})")
    return _back(notice=f"Rule #{rule_number} removed.")


def _back(*, notice: str = "", error: str = ""):
    query = []
    if notice:
        query.append(f"notice={quote(notice)}")
    if error:
        query.append(f"error={quote(error)}")
    suffix = f"?{'&'.join(query)}" if query else ""
    return RedirectResponse(f"/firewall{suffix}", status_code=303)


def _audit(db: OrmSession, session, action: str, target: str | None = None) -> None:
    db.add(
        AuditLog(
            user_id=session.user_id,
            username=session.user.username,
            action=action,
            target=target,
        )
    )
    db.commit()
