"""FastAPI dependencies: authentication, CSRF and template rendering."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
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


def safe_return_to(path: Optional[str], default: str = "/") -> str:
    """Guard against an open redirect: only ever hand back a same-origin path."""
    if path and path.startswith("/") and not path.startswith("//"):
        return path
    return default


def job_redirect(
    job_id: int,
    return_to: str,
    *,
    notice: Optional[str] = None,
    status_code: int = 303,
):
    """Redirect to a job's detail page, tagging it with where the browser
    should go once the job finishes -- see job-detail.js. The job page itself
    stays put on failure so the error is visible; `return_to` (and `notice`,
    forwarded along so a one-time password notice isn't lost) is only ever
    followed once the job actually succeeds.
    """
    params = [f"return_to={quote(safe_return_to(return_to))}"]
    if notice:
        params.append(f"notice={quote(notice)}")
    return RedirectResponse(f"/jobs/{job_id}?{'&'.join(params)}", status_code=status_code)


def render(
    request: Request,
    template: str,
    context: Optional[dict] = None,
    *,
    status_code: int = 200,
    **kwargs,
):
    """Render a template with the values every page needs."""
    from app.services.version import check_for_updates

    payload = {**(context or {}), **kwargs}
    payload.setdefault("session", None)
    payload.setdefault("user", None)
    # Cached check for Lite-Panel self-update (fast memory read)
    payload.setdefault("update_info", check_for_updates())
    # Messages survive a redirect via the query string. Jinja escapes them on
    # the way out, so a crafted link can show a misleading notice at worst,
    # never inject markup.
    payload.setdefault("notice", request.query_params.get("notice"))
    payload.setdefault("error", request.query_params.get("error"))
    return templates.TemplateResponse(request, template, payload, status_code=status_code)

