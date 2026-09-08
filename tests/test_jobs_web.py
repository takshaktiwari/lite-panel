"""HTTP-level tests for job detail and polling endpoints."""

import pytest
from fastapi.testclient import TestClient

from app import jobs
from app.main import create_app
from app.models import Job, JobStatus


@pytest.fixture
def client(db, admin):
    app = create_app()
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client, admin):
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 303
    return client


@pytest.fixture(autouse=True)
def clean_log_buffer():
    jobs.log_buffer._lines.clear()
    yield
    jobs.log_buffer._lines.clear()


def test_job_detail_renders_for_running_job(signed_in, db):
    job = Job(
        kind="test.action",
        description="Ongoing operation",
        status=JobStatus.RUNNING,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Put some lines in buffer
    jobs.log_buffer.append(job.id, "Step 1 complete")
    jobs.log_buffer.append(job.id, "Step 2 in progress")

    resp = signed_in.get(f"/jobs/{job.id}")
    assert resp.status_code == 200
    assert "Ongoing operation" in resp.text
    assert "Step 1 complete" in resp.text
    assert "Step 2 in progress" in resp.text
    assert "job-pulse" in resp.text
    assert "autoscroll-toggle" in resp.text
    assert 'data-live="true"' in resp.text
    assert '/static/job-detail.js' in resp.text


def test_job_detail_renders_for_finished_job(signed_in, db):
    job = Job(
        kind="test.action",
        description="Finished operation",
        status=JobStatus.SUCCESS,
        log="Finished all steps\nDone!",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    resp = signed_in.get(f"/jobs/{job.id}")
    assert resp.status_code == 200
    assert "Finished operation" in resp.text
    assert "Finished all steps" in resp.text
    # When finished, live SSE and polling script is not active
    assert "job-pulse" not in resp.text


def test_job_poll_endpoint(signed_in, db):
    job = Job(
        kind="test.action",
        description="Polling test",
        status=JobStatus.RUNNING,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    jobs.log_buffer.append(job.id, "line 1")
    jobs.log_buffer.append(job.id, "line 2")

    resp = signed_in.get(f"/jobs/{job.id}/poll?offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == job.id
    assert data["status"] == "running"
    assert data["is_terminal"] is False
    assert data["lines"] == ["line 1", "line 2"]
    assert data["next_offset"] == 2

    # Polling with offset returns only incremental lines
    jobs.log_buffer.append(job.id, "line 3")
    resp2 = signed_in.get(f"/jobs/{job.id}/poll?offset=2")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["lines"] == ["line 3"]
    assert data2["next_offset"] == 3


def test_job_poll_terminal_status(signed_in, db):
    job = Job(
        kind="test.action",
        description="Polling terminal test",
        status=JobStatus.FAILED,
        error="Something went wrong",
        log="Execution log before failure",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    resp = signed_in.get(f"/jobs/{job.id}/poll?offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    assert data["is_terminal"] is True
    assert data["error"] == "Something went wrong"
    assert data["log"] == "Execution log before failure"
