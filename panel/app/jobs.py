"""Background jobs.

Installing PHP or issuing a certificate takes minutes, which is far longer
than an HTTP request should live.  Every privileged operation is therefore a
row in ``jobs`` executed by a worker thread, with its output streamed to the
browser as it happens.

A *single* worker thread is intentional, not a simplification: apt holds a
global dpkg lock, so two concurrent installs would fail on each other anyway.
Serialising them here turns a confusing lock error into an orderly queue.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import defaultdict, deque
from typing import Callable, Dict, List, Optional, Tuple

from app.database import session_scope
from app.models import Job, JobStatus, utcnow
from app.shell import stream as shell_stream

logger = logging.getLogger(__name__)

MAX_BUFFERED_LINES = 2000

# kind -> handler.  Providers register their operations at import time.
_handlers: Dict[str, Callable] = {}


def register(kind: str):
    """Decorator registering a job handler under a stable name."""

    def decorator(func: Callable) -> Callable:
        if kind in _handlers:
            raise RuntimeError(f"job kind '{kind}' is already registered")
        _handlers[kind] = func
        return func

    return decorator


class JobLogBuffer:
    """In-memory tail of each running job's output.

    The SSE endpoint reads from here rather than re-querying the database, so
    a chatty ``apt`` run doesn't turn into a write per line per viewer.  The
    authoritative copy is still flushed to the job row.
    """

    def __init__(self) -> None:
        self._lines: Dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_BUFFERED_LINES))
        self._lock = threading.Lock()

    def append(self, job_id: int, line: str) -> None:
        with self._lock:
            self._lines[job_id].append(line)

    def since(self, job_id: int, offset: int) -> List[str]:
        """Lines added after ``offset``, for a client catching up."""
        with self._lock:
            lines = list(self._lines.get(job_id, ()))
        return lines[offset:] if offset < len(lines) else []

    def length(self, job_id: int) -> int:
        with self._lock:
            return len(self._lines.get(job_id, ()))

    def discard(self, job_id: int) -> None:
        with self._lock:
            self._lines.pop(job_id, None)


log_buffer = JobLogBuffer()


class JobProgress:
    """Current/total unit counts for jobs that know their size up front.

    Kept in memory rather than written to the job row on every call: a zip
    with 50,000 entries would otherwise mean 50,000 SQLite commits on the
    single worker thread. The row is updated at a throttled cadence instead
    (see ``JobContext.progress``), which is enough for a browser polling
    every couple of seconds and for a page reloaded mid-job.
    """

    def __init__(self) -> None:
        self._values: Dict[int, Tuple[int, int]] = {}
        self._lock = threading.Lock()

    def set(self, job_id: int, current: int, total: int) -> None:
        with self._lock:
            self._values[job_id] = (current, total)

    def get(self, job_id: int) -> Optional[Tuple[int, int]]:
        with self._lock:
            return self._values.get(job_id)


progress_tracker = JobProgress()

# Minimum time between progress writes to the job row for one job.
PROGRESS_FLUSH_INTERVAL = 0.5


class JobContext:
    """Handed to a job handler: how it logs and how it runs commands."""

    def __init__(self, job_id: int, payload: dict) -> None:
        self.job_id = job_id
        self.payload = payload
        self._pending: List[str] = []
        self._progress: Optional[Tuple[int, int]] = None
        self._progress_flushed_at: float = 0.0

    def log(self, line: str) -> None:
        """Record one line of progress, visible to the browser immediately."""
        text = str(line).rstrip("\n")
        log_buffer.append(self.job_id, text)
        self._pending.append(text)
        if len(self._pending) >= 20:
            self.flush()

    def progress(self, current: int, total: int) -> None:
        """Update this job's progress bar.

        Cheap enough to call on every unit of work -- the in-memory tracker
        is what the browser actually polls. The job row itself is only
        written at most every ``PROGRESS_FLUSH_INTERVAL`` seconds, plus
        always on the final call (``current >= total``), so the finished
        value is never stale.
        """
        progress_tracker.set(self.job_id, current, total)
        self._progress = (current, total)
        now = time.monotonic()
        if current >= total or now - self._progress_flushed_at >= PROGRESS_FLUSH_INTERVAL:
            self._progress_flushed_at = now
            self._flush_progress()

    def _flush_progress(self) -> None:
        if self._progress is None:
            return
        current, total = self._progress
        with session_scope() as db:
            job = db.get(Job, self.job_id)
            if job is not None:
                job.progress_current = current
                job.progress_total = total

    def run(self, args, **kwargs) -> int:
        """Run a command, streaming its output into the job log."""
        self.log(f"$ {' '.join(str(a) for a in args)}")
        code = shell_stream(args, self.log, **kwargs)
        if code != 0:
            self.log(f"[exit {code}]")
        return code

    def check(self, args, **kwargs) -> None:
        """Run a command and abort the job if it fails."""
        code = self.run(args, **kwargs)
        if code != 0:
            raise JobFailed(f"{args[0]} exited {code}")

    def flush(self) -> None:
        """Persist buffered lines onto the job row."""
        if not self._pending:
            return
        chunk = "\n".join(self._pending) + "\n"
        self._pending.clear()
        with session_scope() as db:
            job = db.get(Job, self.job_id)
            if job is not None:
                job.log = (job.log or "") + chunk


class JobFailed(RuntimeError):
    """Raised by a handler to fail its job with a readable message."""


class JobWorker:
    """Consumes queued jobs on a single background thread."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="job-worker", daemon=True)
        self._thread.start()
        logger.info("job worker started")

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=5)

    def submit(self, job_id: int) -> None:
        self._queue.put(job_id)

    def _loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._queue.get()
            if job_id is None:
                break
            try:
                self._execute(job_id)
            except Exception:  # a crash here must not take the worker down
                logger.exception("job %s crashed the worker loop", job_id)
            finally:
                self._queue.task_done()

    def _execute(self, job_id: int) -> None:
        with session_scope() as db:
            job = db.get(Job, job_id)
            if job is None:
                logger.warning("job %s disappeared before it ran", job_id)
                return
            kind, payload_raw = job.kind, job.payload
            job.status = JobStatus.RUNNING
            job.started_at = utcnow()

        handler = _handlers.get(kind)
        ctx = JobContext(job_id, _decode_payload(payload_raw))

        if handler is None:
            self._finish(ctx, JobStatus.FAILED, f"No handler registered for job kind '{kind}'.")
            return

        try:
            handler(ctx)
        except JobFailed as exc:
            ctx.log(f"error: {exc}")
            self._finish(ctx, JobStatus.FAILED, str(exc))
        except Exception as exc:  # noqa: BLE001 - the job row is where this surfaces
            logger.exception("job %s (%s) failed", job_id, kind)
            ctx.log(f"error: {exc}")
            self._finish(ctx, JobStatus.FAILED, str(exc))
        else:
            self._finish(ctx, JobStatus.SUCCESS, None)

    def _finish(self, ctx: JobContext, status: JobStatus, error: Optional[str]) -> None:
        ctx.flush()
        with session_scope() as db:
            job = db.get(Job, ctx.job_id)
            if job is not None:
                job.status = status
                job.error = error
                job.finished_at = utcnow()
        logger.info("job %s finished: %s", ctx.job_id, status.value)


worker = JobWorker()


def enqueue(
    db,
    kind: str,
    description: str,
    *,
    payload: Optional[dict] = None,
    user_id: Optional[int] = None,
) -> Job:
    """Create a job row and hand it to the worker."""
    if kind not in _handlers:
        raise ValueError(f"unknown job kind '{kind}'")

    job = Job(
        kind=kind,
        description=description,
        payload=json.dumps(payload or {}),
        started_by=user_id,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    worker.submit(job.id)
    return job


def _decode_payload(raw: Optional[str]) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def registered_kinds() -> List[str]:
    return sorted(_handlers)
