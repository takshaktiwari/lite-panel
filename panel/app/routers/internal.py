"""Endpoints nginx calls, not the browser."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session as OrmSession

from app import security
from app.database import get_session
from app.deps import optional_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal", include_in_schema=False)


@router.get("/auth-check")
def auth_check(session=Depends(optional_session)) -> Response:
    """The gate in front of Adminer.

    nginx's ``auth_request`` calls this before proxying anything under
    ``/adminer/``, forwarding the browser's cookies, and passes the request
    through only on a 2xx.  Without it that path would be reachable by anyone
    who typed the URL, since nothing else in nginx knows about panel sessions.

    Returns no body: nginx discards it, and an empty 401 keeps the response
    cheap for a check that runs on every asset the tool loads.
    """
    if session is None:
        return Response(status_code=401)
    return Response(status_code=204)


@router.get("/session-user")
def session_user(
    session=Depends(optional_session),
    db: OrmSession = Depends(get_session),
) -> Response:
    """Identify the signed-in admin to nginx, for logging."""
    if session is None:
        return Response(status_code=401)
    return Response(status_code=204, headers={"X-Panel-User": session.user.username})


__all__ = ["router", "security"]
