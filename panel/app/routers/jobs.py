"""Job list, job detail, and the live log stream."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.database import SessionLocal, get_session
from app.deps import render, require_session
from app.jobs import log_buffer
from app.models import Job

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs")

POLL_INTERVAL = 0.4
# Long enough that a slow apt install does not look stalled, short enough that
# a dropped connection is noticed. The browser reconnects on its own.
STREAM_TIMEOUT = 3600


@router.get("")
def job_list(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    jobs = db.scalars(select(Job).order_by(Job.id.desc()).limit(50)).all()
    return render(request, "jobs/list.html", session=session, user=session.user, jobs=jobs)


@router.get("/{job_id}")
def job_detail(
    job_id: int,
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    job = db.get(Job, job_id)
    if job is None:
        return render(
            request,
            "error.html",
            session=session,
            user=session.user,
            message="That job does not exist.",
            status_code=404,
        )

    # A finished job renders its stored log. A running one starts empty and
    # fills from the stream, so lines are never shown twice.
    return render(
        request,
        "jobs/detail.html",
        session=session,
        user=session.user,
        job=job,
        static_log=job.log if job.is_terminal else "",
        live=not job.is_terminal,
    )


@router.get("/{job_id}/stream")
async def job_stream(job_id: int, session=Depends(require_session)):
    """Server-sent events carrying new log lines as they are produced.

    SSE rather than websockets: the traffic is one-directional and this needs
    no client library, no upgrade handshake, and reconnects by itself.
    """

    async def events():
        cursor = 0
        waited = 0.0

        while waited < STREAM_TIMEOUT:
            lines = log_buffer.since(job_id, cursor)
            if lines:
                cursor += len(lines)
                yield f"data: {json.dumps({'lines': lines})}\n\n"

            status = _job_status(job_id)
            if status in ("success", "failed"):
                # Drain anything written between the read above and now.
                remaining = log_buffer.since(job_id, cursor)
                if remaining:
                    yield f"data: {json.dumps({'lines': remaining})}\n\n"
                yield f"event: done\ndata: {json.dumps({'status': status})}\n\n"
                return

            await asyncio.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL

        yield f"event: done\ndata: {json.dumps({'status': 'timeout'})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx buffers proxied responses by default, which would hold the
            # entire stream until the job finished -- defeating the point.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _job_status(job_id: int) -> str:
    """Read a job's status on its own short-lived session.

    The request's session is not reused here: it would hold one SQLite
    connection open for the whole life of the stream.
    """
    session = SessionLocal()
    try:
        job = session.get(Job, job_id)
        return job.status.value if job else "failed"
    finally:
        session.close()
