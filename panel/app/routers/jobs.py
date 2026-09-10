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
from app.deps import render, require_session, safe_return_to
from app.jobs import log_buffer, progress_tracker
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

    # For finished jobs, render full stored log.
    # For running jobs, render whatever has been buffered so far so the user
    # sees immediate context even before new events arrive.
    if job.is_terminal:
        static_log = job.log
    else:
        buffered_lines = log_buffer.since(job_id, 0)
        static_log = "\n".join(buffered_lines) + ("\n" if buffered_lines else "")

    raw_return_to = request.query_params.get("return_to")
    return_to = safe_return_to(raw_return_to, default="") or None

    if job.is_terminal:
        progress = (job.progress_current, job.progress_total)
    else:
        progress = progress_tracker.get(job_id) or (job.progress_current, job.progress_total)

    return render(
        request,
        "jobs/detail.html",
        session=session,
        user=session.user,
        job=job,
        static_log=static_log,
        live=not job.is_terminal,
        return_to=return_to,
        progress_current=progress[0],
        progress_total=progress[1],
    )


@router.get("/{job_id}/poll")
def job_poll(
    job_id: int,
    offset: int = 0,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    """JSON polling endpoint for job status and incremental log lines.

    Used alongside or as a fallback to the SSE stream. Allows client-side auto-update
    every 2-3s even if SSE drops, and informs the UI about status changes live.
    """
    job = db.get(Job, job_id)
    if job is None:
        return {"error": "Not found", "is_terminal": True}

    lines = log_buffer.since(job_id, offset)
    # If the buffer has no lines for this offset (e.g. after finish or buffer discard),
    # fall back to persisted log on the job row.
    log_text = job.log if job.is_terminal and not lines else None

    if job.is_terminal:
        progress = (job.progress_current, job.progress_total)
    else:
        progress = progress_tracker.get(job_id) or (job.progress_current, job.progress_total)

    return {
        "id": job.id,
        "status": job.status.value,
        "is_terminal": job.is_terminal,
        "error": job.error,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "lines": lines,
        "next_offset": offset + len(lines),
        "log": log_text,
        "progress_current": progress[0],
        "progress_total": progress[1],
    }


@router.get("/{job_id}/stream")
async def job_stream(job_id: int, session=Depends(require_session)):
    """Server-sent events carrying new log lines as they are produced.

    SSE rather than websockets: the traffic is one-directional and this needs
    no client library, no upgrade handshake, and reconnects by itself.
    """

    async def events():
        cursor = 0
        waited = 0.0
        last_progress = None

        while waited < STREAM_TIMEOUT:
            payload: dict = {}

            lines = log_buffer.since(job_id, cursor)
            if lines:
                cursor += len(lines)
                payload["lines"] = lines

            progress = progress_tracker.get(job_id)
            if progress is not None and progress != last_progress:
                last_progress = progress
                payload["progress_current"], payload["progress_total"] = progress

            if payload:
                yield f"data: {json.dumps(payload)}\n\n"

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
