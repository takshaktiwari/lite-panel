"""FastAPI dependencies: authentication, CSRF and template rendering."""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session as OrmSession

from app import security
from app.config import get_settings
from app.database import get_session
from app.models import AdminUser, Session

settings = get_settings()

templates = Jinja2Templates(directory=str(settings.web_templates_dir))
templates.env.globals["app_name"] = settings.app_name


class NotAuthenticated(Exception):
    """Raised when a page needs a login.  Turned into a redirect in main.py."""


class CsrfError(Exception):
    """Raised when a mutating request arrives without a valid CSRF token."""


def optional_session(
    request: Request, db: OrmSession = Depends(get_session)
) -> Optional[Session]:
    """The current session, or None.  For pages that render either way."""
    token = request.cookies.get(security.SESSION_COOKIE)
    if not token:
        return None
    return security.get_session(db, token)


def require_session(session: Optional[Session] = Depends(optional_session)) -> Session:
    if session is None:
        raise NotAuthenticated()
    return session


def require_user(session: Session = Depends(require_session)) -> AdminUser:
    return session.user


async def csrf_protect(request: Request, session: Session = Depends(require_session)) -> None:
    """Verify the CSRF token on any request that changes state.

    Starlette caches the parsed form on the request, so consuming it here does
    not stop the route handler from reading it again.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    form = await request.form()
    submitted = form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not security.constant_time_equals(str(submitted or ""), session.csrf_token):
        raise CsrfError()


def client_ip(request: Request) -> str:
    """The caller's address.

    nginx is the only thing that talks to this daemon, so X-Forwarded-For is
    set by us and can be trusted -- but only its first hop, and only because
    the socket is bound to loopback and cannot be reached directly.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "unknown")[:45]


def render(
    request: Request,
    template: str,
    context: Optional[dict] = None,
    *,
    status_code: int = 200,
    **kwargs,
):
    """Render a template with the values every page needs."""
    payload = {**(context or {}), **kwargs}
    payload.setdefault("session", None)
    payload.setdefault("user", None)
    # Messages survive a redirect via the query string. Jinja escapes them on
    # the way out, so a crafted link can show a misleading notice at worst,
    # never inject markup.
    payload.setdefault("notice", request.query_params.get("notice"))
    payload.setdefault("error", request.query_params.get("error"))
    return templates.TemplateResponse(request, template, payload, status_code=status_code)
