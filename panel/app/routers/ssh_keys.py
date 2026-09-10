"""SSH Access router.

Allows generating SSH key pairs, importing public keys, downloading private keys,
and revoking keys for both root and site system accounts.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.database import get_session
from app.deps import csrf_protect, render, require_session
from app.models import AuditLog, Site, SshKey
from app.services import ssh_keys as ssh_service
from app.services import system
from app.validators import ValidationError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ssh")


def _back(*, notice: Optional[str] = None, error: Optional[str] = None):
    params = []
    if notice:
        params.append(f"notice={quote(notice)}")
    if error:
        params.append(f"error={quote(error)}")
    suffix = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(f"/ssh{suffix}", status_code=303)


def _audit(db: OrmSession, session, action: str, target: Optional[str]) -> None:
    db.add(
        AuditLog(
            user_id=session.user_id,
            username=session.user.username,
            action=action,
            target=(target or "")[:255],
        )
    )
    db.commit()


@router.get("")
def ssh_list(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    keys = db.scalars(select(SshKey).order_by(SshKey.system_user, SshKey.name)).all()
    sites = db.scalars(select(Site).where(Site.is_active).order_by(Site.domain)).all()

    return render(
        request,
        "ssh/list.html",
        session=session,
        user=session.user,
        keys=keys,
        sites=sites,
        ssh_available=ssh_service.is_available(),
        public_ip=system.public_ip() or "your-server-ip",
    )


@router.post("/generate", dependencies=[Depends(csrf_protect)])
def generate_key(
    name: str = Form(""),
    target_user: str = Form("root"),
    key_type: str = Form("ed25519"),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    name = name.strip()
    if not name:
        return _back(error="Key name is required.")

    # Resolve system_user and optional site_id
    site_id = None
    if target_user == "root":
        system_user = "root"
    else:
        try:
            s_id = int(target_user)
            site = db.get(Site, s_id)
            if not site:
                return _back(error="Selected site does not exist.")
            system_user = site.system_user
            site_id = site.id
        except (ValueError, TypeError):
            return _back(error="Invalid user selection.")

    try:
        priv_key, pub_key, fingerprint = ssh_service.generate_key_pair(
            comment=f"{name} ({system_user})",
            key_type=key_type,
        )
        ssh_service.create_key(
            db,
            name=name,
            system_user=system_user,
            public_key=pub_key,
            site_id=site_id,
        )
    except ValidationError as exc:
        return _back(error=str(exc))
    except Exception as exc:
        logger.error("Failed to generate SSH key: %s", exc)
        return _back(error=f"Key generation failed: {exc}")

    _audit(db, session, "ssh.generate", f"{name} ({system_user})")

    # Download the private key immediately with appropriate headers
    filename = f"{name.replace(' ', '_')}_{system_user}.pem"
    return Response(
        content=priv_key,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import", dependencies=[Depends(csrf_protect)])
def import_key(
    name: str = Form(""),
    target_user: str = Form("root"),
    public_key: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    name = name.strip()
    if not name:
        return _back(error="Key name is required.")
    public_key = public_key.strip()
    if not public_key:
        return _back(error="Public key cannot be empty.")

    site_id = None
    if target_user == "root":
        system_user = "root"
    else:
        try:
            s_id = int(target_user)
            site = db.get(Site, s_id)
            if not site:
                return _back(error="Selected site does not exist.")
            system_user = site.system_user
            site_id = site.id
        except (ValueError, TypeError):
            return _back(error="Invalid user selection.")

    try:
        key_record = ssh_service.create_key(
            db,
            name=name,
            system_user=system_user,
            public_key=public_key,
            site_id=site_id,
        )
    except ValidationError as exc:
        return _back(error=str(exc))
    except Exception as exc:
        logger.error("Failed to import SSH key: %s", exc)
        return _back(error=f"Key import failed: {exc}")

    _audit(db, session, "ssh.import", f"{name} ({system_user})")
    return _back(notice=f"SSH public key '{key_record.name}' added for {system_user}.")


@router.get("/{key_id}/public-key")
def get_public_key(
    key_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    key_record = db.get(SshKey, key_id)
    if not key_record:
        return PlainTextResponse("Key not found", status_code=404)
    return PlainTextResponse(key_record.public_key)


@router.post("/{key_id}/delete", dependencies=[Depends(csrf_protect)])
def delete_key(
    key_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    key_record = db.get(SshKey, key_id)
    if not key_record:
        return _back(error="That SSH key no longer exists.")

    name = key_record.name
    system_user = key_record.system_user
    ssh_service.delete_key(db, key_record)

    _audit(db, session, "ssh.delete", f"{name} ({system_user})")
    return _back(notice=f"SSH key '{name}' for {system_user} revoked.")
