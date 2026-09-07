"""Application factory and process lifecycle."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import get_settings
from app.database import init_db, session_scope
from app.deps import CsrfError, NotAuthenticated, render
from app.jobs import worker
from app.models import Job, JobStatus
from app.routers import (
    auth,
    dashboard,
    databases,
    files,
    ftp,
    internal,
    jobs as jobs_router,
    setup,
    sites,
    stack,
)
from app.security import purge_expired_sessions

# Importing this registers every job handler with the worker.
from app import tasks  # noqa: F401  isort:skip

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.DEBUG if settings.dev_mode else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logger.info("starting %s %s", settings.app_name, __version__)

    init_db()
    with session_scope() as db:
        removed = purge_expired_sessions(db)
        if removed:
            logger.info("purged %s expired sessions", removed)

        # Any job still marked RUNNING from a previous process is now orphaned
        # — the worker thread that was executing it is gone.  Mark them FAILED
        # so the SSE stream emits a `done` event and the browser reloads the
        # job page to show whatever log was flushed to the DB before the crash.
        from sqlalchemy import select as _select
        orphaned = db.scalars(
            _select(Job).where(Job.status == JobStatus.RUNNING)
        ).all()
        for job in orphaned:
            job.status = JobStatus.FAILED
            job.error = "Service restarted while this job was running. Check the log above for partial output."
            logger.warning("marked orphaned job %s (%s) as failed", job.id, job.kind)

    worker.start()
    try:
        yield
    finally:
        worker.stop()
        logger.info("stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        # The panel is an internal tool behind a login; there is no reason to
        # publish an interactive API explorer on it.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(setup.router)
    app.include_router(sites.router)
    app.include_router(databases.router)
    app.include_router(ftp.router)
    app.include_router(files.router)
    app.include_router(stack.router)
    app.include_router(jobs_router.router)
    app.include_router(internal.router)

    @app.exception_handler(NotAuthenticated)
    async def _needs_login(request: Request, _exc: NotAuthenticated):
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(CsrfError)
    async def _bad_csrf(request: Request, _exc: CsrfError):
        logger.warning("CSRF rejection for %s %s", request.method, request.url.path)
        return render(
            request,
            "error.html",
            message="That request could not be verified. Please reload the page and try again.",
            status_code=400,
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # The panel serves no third-party assets, so it can afford a strict
        # policy; 'unsafe-inline' covers the small style attributes used for
        # meter widths.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
        )
        return response

    return app


app = create_app()
