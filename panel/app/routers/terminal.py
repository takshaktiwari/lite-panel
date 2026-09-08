"""The web terminal: the page and the websocket bridge.

A terminal is the one place in this app where "unstructured root shell" is
the point rather than the thing being guarded against -- see
app/services/terminal.py for why that module is allowed to spawn a real
shell when nothing else in the codebase is. A valid panel session is enough
to open one (the same bar as every other page); the websocket additionally
only accepts same-origin connections, and every open/close is logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from typing import Optional

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session as OrmSession

from app import security
from app.config import get_settings
from app.database import SessionLocal
from app.deps import render, require_session
from app.models import AuditLog, Session
from app.services.terminal import TerminalSession

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()

# Bytes read from the pty per event-loop wakeup. Generous enough that a fast
# `cat largefile` doesn't trickle, small enough not to hog the loop.
_READ_CHUNK = 65536


@router.get("/terminal")
def terminal_page(
    request: Request,
    session: Session = Depends(require_session),
):
    return render(
        request,
        "terminal.html",
        session=session,
        user=session.user,
        idle_timeout_minutes=settings.terminal_idle_timeout_minutes,
    )


# --------------------------------------------------------------------------
# The websocket bridge
# --------------------------------------------------------------------------


def _allowed_origin(websocket: WebSocket) -> bool:
    """Reject cross-origin websocket connections.

    Unlike a form POST, a browser attaches cookies to a cross-origin
    WebSocket handshake too -- CSRF tokens don't apply here since there's no
    form body to carry one. Pinning Origin to the Host the request actually
    arrived on is the control that takes its place.
    """
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not origin or not host:
        return False
    return origin in (f"https://{host}", f"http://{host}")


def _session_from_cookie(db: OrmSession, websocket: WebSocket) -> Optional[Session]:
    token = websocket.cookies.get(security.SESSION_COOKIE)
    if not token:
        return None
    return security.get_session(db, token)


@router.websocket("/terminal/ws")
async def terminal_ws(websocket: WebSocket) -> None:
    if not _allowed_origin(websocket):
        logger.warning("terminal ws rejected: origin=%s host=%s",
                        websocket.headers.get("origin"), websocket.headers.get("host"))
        await websocket.close(code=4403)
        return

    db = SessionLocal()
    try:
        session = _session_from_cookie(db, websocket)
        if session is None:
            await websocket.close(code=4401)
            return

        username = session.user.username
        user_id = session.user_id
        ip = client_ip_from_ws(websocket)
    finally:
        db.close()

    await websocket.accept()
    _log_event("terminal.open", user_id, username, ip)

    loop = asyncio.get_event_loop()
    # /root is where the daemon actually runs on a real (Linux) install; it
    # doesn't exist on macOS, so this falls back to the service's own
    # portable default there rather than failing every terminal in dev/CI.
    term = TerminalSession(cwd="/root" if os.path.isdir("/root") else None)
    last_activity = loop.time()
    idle_seconds = settings.terminal_idle_timeout_minutes * 60

    output_queue: asyncio.Queue = asyncio.Queue()

    # A dedicated thread doing a blocking select()+read() on the pty, rather
    # than loop.add_reader() -- deliberately. add_reader() is documented to
    # work only with genuine sockets on some event loop implementations, and
    # in practice under uvloop (which uvicorn[standard] installs and prefers
    # automatically) a reader registered on a pty master fd fired once and
    # then silently stopped firing: the first echoed keystroke came through
    # and nothing the shell produced afterward ever did, confirmed against a
    # real deployment where reading the same fd directly, outside asyncio,
    # worked instantly. A plain OS thread blocking in select()/read() has no
    # dependency on which reactor is driving the event loop, which is what
    # makes this portable across asyncio's default loop and uvloop alike.
    stop_reading = threading.Event()

    def _reader_thread() -> None:
        import select as _select

        # The loop's only exit signal is a real EOF: select() reports the fd
        # readable, and the read that follows comes back empty. That is the
        # actual POSIX end-of-file condition for a pty (every process holding
        # the slave side has exited) and it is the one thing this loop trusts.
        #
        # It deliberately does NOT exit on Popen.poll() alone. That was tried
        # first and caused a real, reproducible bug: this box's shell prints
        # its own "start of command" / "end of command" tracking markers
        # (OSC sequences with a machine id / boot id / pid), and immediately
        # after reading one of those, poll() briefly reported the shell as
        # exited even though it demonstrably was not -- a parallel real
        # session hit the identical marker over and over and kept working
        # for hundreds of read cycles. Whatever produces that momentary
        # poll() result, treating it as final ended a terminal session out
        # from under a still-live shell. EOF has no such race: it only ever
        # happens once every process on the slave side is actually gone.
        while not stop_reading.is_set():
            try:
                ready, _, _ = _select.select([term.master_fd], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            chunk = term.read(_READ_CHUNK)
            if chunk:
                loop.call_soon_threadsafe(output_queue.put_nowait, chunk)
                continue
            # select() said readable, read() came back with nothing: real EOF.
            break
        loop.call_soon_threadsafe(output_queue.put_nowait, None)  # sentinel

    reader_thread = threading.Thread(target=_reader_thread, daemon=True)
    reader_thread.start()

    async def pump_output() -> None:
        while True:
            chunk = await output_queue.get()
            if chunk is None:
                await websocket.close(code=1000)
                return
            await websocket.send_bytes(chunk)

    output_task = asyncio.create_task(pump_output())

    try:
        while True:
            remaining = idle_seconds - (loop.time() - last_activity)
            if remaining <= 0:
                try:
                    await websocket.send_text(json.dumps({"type": "timeout"}))
                except Exception:  # noqa: BLE001 - best-effort notice
                    pass
                break

            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                continue

            if message["type"] == "websocket.disconnect":
                break

            if "bytes" in message and message["bytes"] is not None:
                term.write(message["bytes"])
                last_activity = loop.time()
            elif "text" in message and message["text"] is not None:
                _handle_control_message(term, message["text"])
                last_activity = loop.time()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - a bug here must still release the pty
        logger.exception("terminal websocket loop crashed")
    finally:
        stop_reading.set()
        term.close()  # closes master_fd too, which unblocks a pending select()
        reader_thread.join(timeout=2)
        output_task.cancel()
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - already closing/closed
            pass
        _log_event("terminal.close", user_id, username, ip)


def _handle_control_message(term: TerminalSession, raw: str) -> None:
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        return
    if not isinstance(message, dict):
        return
    if message.get("type") == "resize":
        cols, rows = message.get("cols"), message.get("rows")
        if isinstance(cols, int) and isinstance(rows, int):
            term.resize(cols, rows)


def client_ip_from_ws(websocket: WebSocket) -> str:
    forwarded = websocket.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return (websocket.client.host if websocket.client else "unknown")[:45]


def _log_event(action: str, user_id: int, username: str, ip: str) -> None:
    db = SessionLocal()
    try:
        db.add(AuditLog(action=action, user_id=user_id, username=username, ip_address=ip))
        db.commit()
    finally:
        db.close()
