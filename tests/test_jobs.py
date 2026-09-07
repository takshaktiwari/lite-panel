"""Tests for the background job runner."""

import time

import pytest

from app import jobs
from app.models import Job, JobStatus


def _wait_for(job_id, db, timeout=10):
    """Poll until the job reaches a terminal state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        db.expire_all()
        job = db.get(Job, job_id)
        if job and job.is_terminal:
            return job
        time.sleep(0.05)
    pytest.fail(f"job {job_id} did not finish within {timeout}s")


@pytest.fixture(autouse=True)
def running_worker():
    jobs.worker.start()
    yield


def test_successful_job_runs_and_logs(db):
    @jobs.register("test_success")
    def _handler(ctx):
        ctx.log("working")
        ctx.log(f"got {ctx.payload['value']}")

    job = jobs.enqueue(db, "test_success", "A test job", payload={"value": 42})
    finished = _wait_for(job.id, db)

    assert finished.status == JobStatus.SUCCESS
    assert finished.error is None
    assert "working" in finished.log
    assert "got 42" in finished.log
    assert finished.started_at and finished.finished_at


def test_failing_job_is_recorded_not_raised(db):
    @jobs.register("test_failure")
    def _handler(ctx):
        raise jobs.JobFailed("could not reticulate splines")

    job = jobs.enqueue(db, "test_failure", "A doomed job")
    finished = _wait_for(job.id, db)

    assert finished.status == JobStatus.FAILED
    assert "reticulate" in finished.error


def test_unexpected_exception_fails_the_job_without_killing_the_worker(db):
    @jobs.register("test_crash")
    def _handler(ctx):
        raise RuntimeError("boom")

    @jobs.register("test_after_crash")
    def _handler2(ctx):
        ctx.log("still alive")

    crashed = jobs.enqueue(db, "test_crash", "Crashing job")
    assert _wait_for(crashed.id, db).status == JobStatus.FAILED

    # The worker must survive to run the next job.
    later = jobs.enqueue(db, "test_after_crash", "Later job")
    assert _wait_for(later.id, db).status == JobStatus.SUCCESS


def test_job_can_run_a_real_command(db):
    @jobs.register("test_command")
    def _handler(ctx):
        ctx.check(["echo", "hello from a job"])

    job = jobs.enqueue(db, "test_command", "Command job")
    finished = _wait_for(job.id, db)

    assert finished.status == JobStatus.SUCCESS
    assert "hello from a job" in finished.log


def test_failing_command_fails_the_job(db):
    @jobs.register("test_bad_command")
    def _handler(ctx):
        ctx.check(["false"])

    job = jobs.enqueue(db, "test_bad_command", "Bad command job")
    assert _wait_for(job.id, db).status == JobStatus.FAILED


def test_enqueueing_an_unknown_kind_is_refused(db):
    with pytest.raises(ValueError):
        jobs.enqueue(db, "no_such_kind", "Nope")


def test_registering_a_duplicate_kind_is_refused():
    @jobs.register("test_duplicate")
    def _handler(ctx):
        pass

    with pytest.raises(RuntimeError):

        @jobs.register("test_duplicate")
        def _other(ctx):
            pass


def test_log_buffer_serves_incremental_reads(db):
    """What the SSE endpoint relies on: fetch only what's new."""
    buffer = jobs.JobLogBuffer()
    buffer.append(1, "first")
    buffer.append(1, "second")

    assert buffer.since(1, 0) == ["first", "second"]
    assert buffer.since(1, 2) == []

    buffer.append(1, "third")
    assert buffer.since(1, 2) == ["third"]
    assert buffer.length(1) == 3

    buffer.discard(1)
    assert buffer.length(1) == 0


def test_payload_defaults_to_empty_dict_when_malformed():
    assert jobs._decode_payload(None) == {}
    assert jobs._decode_payload("not json") == {}
    assert jobs._decode_payload("[1,2]") == {}
    assert jobs._decode_payload('{"a": 1}') == {"a": 1}
