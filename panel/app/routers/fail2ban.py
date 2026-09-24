"""Fail2ban router: install, settings, and banning/unbanning IPs."""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as OrmSession

from app.database import get_session
from app.deps import client_ip, csrf_protect, job_redirect, render, require_session
from app.jobs import enqueue
from app.models import AuditLog
from app.services import fail2ban as f2b
from app.validators import ValidationError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/fail2ban")


@router.get("")
def fail2ban_page(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    installed = f2b.is_installed()
    settings = f2b.get_settings(db)
    status = {"running": False, "jails": {}, "bans": [], "total_failed": 0,
              "total_banned": 0, "broken_jails": []}
    ssh = f2b.SshInfo()

    if installed:
        try:
            status = f2b.get_status()
            if status["running"]:
                # Self-heal: a permanent ban lost to a wiped fail2ban state
                # comes back the next time anyone looks at this page.
                if f2b.reapply_permanent_bans(db):
                    status = f2b.get_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read fail2ban status: %s", exc)
        ssh = f2b.detect_ssh()

    return render(
        request,
        "fail2ban/index.html",
        session=session,
        user=session.user,
        installed=installed,
        status=status,
        settings=settings,
        ignoreip_entries=f2b.parse_ignoreip(settings.ignoreip),
        always_ignored=f2b.ALWAYS_IGNORED,
        permanent_notes={row.ip: row.note for row in f2b.list_permanent_bans(db)},
        ssh=ssh,
        my_ip=client_ip(request),
        findtime_choices=_with_current(f2b.FINDTIME_CHOICES, settings.findtime),
        bantime_choices=_with_current(f2b.BANTIME_CHOICES, settings.bantime),
        maxtime_choices=_with_current(f2b.MAXTIME_CHOICES, settings.increment_maxtime),
        format_duration=f2b.format_duration,
    )


@router.post("/install", dependencies=[Depends(csrf_protect)])
def install(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    # The admin installing it is the one person who must never get locked out.
    f2b.add_ignoreip(db, client_ip(request))
    job = enqueue(db, "fail2ban.install", "Install fail2ban", payload={}, user_id=session.user_id)
    return job_redirect(job.id, "/fail2ban")


@router.post("/uninstall", dependencies=[Depends(csrf_protect)])
def uninstall(
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    job = enqueue(db, "fail2ban.uninstall", "Uninstall fail2ban", payload={}, user_id=session.user_id)
    return job_redirect(job.id, "/fail2ban")


@router.post("/settings", dependencies=[Depends(csrf_protect)])
def save_settings(
    sshd_enabled: str = Form(""),
    maxretry: int = Form(...),
    findtime: int = Form(...),
    bantime: int = Form(...),
    increment_enabled: str = Form(""),
    increment_maxtime: int = Form(...),
    recidive_enabled: str = Form(""),
    ignoreip: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        f2b.update_settings(
            db,
            sshd_enabled=bool(sshd_enabled),
            maxretry=maxretry,
            findtime=findtime,
            bantime=bantime,
            increment_enabled=bool(increment_enabled),
            increment_maxtime=increment_maxtime,
            recidive_enabled=bool(recidive_enabled),
            ignoreip=ignoreip,
        )
    except ValidationError as exc:
        db.rollback()
        return _back(error=str(exc))

    if not f2b.is_installed():
        return _back(notice="Settings saved. They'll be applied when fail2ban is installed.")

    try:
        f2b.apply_config(db)
    except Exception as exc:  # noqa: BLE001
        return _back(error=f"Settings saved but not applied: {exc}")

    _audit(db, session, "fail2ban.settings")
    return _back(notice="Settings saved and applied.")


@router.post("/repair", dependencies=[Depends(csrf_protect)])
def repair(
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        f2b.apply_config(db, force_restart=True)
    except Exception as exc:  # noqa: BLE001
        return _back(error=f"Repair failed: {exc}")

    _audit(db, session, "fail2ban.repair")
    return _back(notice="fail2ban restarted and is blocking again.")


@router.post("/ban", dependencies=[Depends(csrf_protect)])
def ban(
    request: Request,
    ip: str = Form(...),
    permanent: str = Form(""),
    note: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    is_permanent = bool(permanent)
    try:
        banned = f2b.ban_ip(db, ip, permanent=is_permanent, note=note,
                            protected=[client_ip(request)])
    except ValidationError as exc:
        return _back(error=str(exc))
    except Exception as exc:  # noqa: BLE001
        return _back(error=f"Could not ban {ip.strip()}: {exc}")

    _audit(db, session, "fail2ban.ban", f"{banned} ({'permanent' if is_permanent else 'ssh jail'})")
    kind = "permanently, on every port" if is_permanent else "from SSH"
    return _back(notice=f"Banned {banned} {kind}.")


@router.post("/unban", dependencies=[Depends(csrf_protect)])
def unban(
    ip: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        unbanned = f2b.unban_ip(db, ip)
    except ValidationError as exc:
        return _back(error=str(exc))

    _audit(db, session, "fail2ban.unban", unbanned)
    return _back(notice=f"Unbanned {unbanned}.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _with_current(choices: list, current: int) -> list:
    """The preset list, plus the stored value if it isn't one of them, so the
    form never silently changes a setting on the next save."""
    if any(value == current for value, _ in choices):
        return choices
    return sorted(choices + [(current, f2b.format_duration(current))])


def _audit(db: OrmSession, session, action: str, target: str | None = None) -> None:
    db.add(AuditLog(user_id=session.user_id, username=session.user.username,
                    action=action, target=target))
    db.commit()


def _back(*, notice: str = "", error: str = "") -> RedirectResponse:
    query = []
    if notice:
        query.append(f"notice={quote(notice)}")
    if error:
        query.append(f"error={quote(error)}")
    suffix = f"?{'&'.join(query)}" if query else ""
    return RedirectResponse(f"/fail2ban{suffix}", status_code=303)
