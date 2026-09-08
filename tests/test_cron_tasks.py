"""Tests for the "cron.sync" job handler (app.tasks.sync_cron).

This is the piece that decides which crontab a change actually lands in --
a site's own system user's, or root's when the job has no site. shell.run
is monkeypatched throughout so nothing here touches a real crontab.
"""

import pytest

from app import tasks
from app.models import CronJob, Site
from app.services import cron as cron_service


class _FakeCtx:
    def __init__(self, payload):
        self.payload = payload
        self.lines = []

    def log(self, line):
        self.lines.append(str(line))


@pytest.fixture(autouse=True)
def fake_crontab(monkeypatch):
    calls = []
    monkeypatch.setattr(cron_service, "is_available", lambda: True)
    monkeypatch.setattr(
        cron_service,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )
    return calls


@pytest.fixture
def site(db):
    site = Site(
        name="demo",
        domain="demo.example.com",
        system_user="site_demo",
        root_dir="/var/www/demo",
        webroot="/var/www/demo",
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def test_sync_cron_installs_into_the_sites_own_user(db, site, fake_crontab):
    db.add(
        CronJob(
            site_id=site.id,
            minute="*", hour="*", day_of_month="*", month="*", day_of_week="*",
            command="echo site",
        )
    )
    db.commit()

    tasks.sync_cron(_FakeCtx({"site_id": site.id}))

    assert len(fake_crontab) == 1
    args, kwargs = fake_crontab[0]
    assert args == ["crontab", "-u", "site_demo", "-"]
    assert "echo site" in kwargs["input"]


def test_sync_cron_installs_into_root_when_site_id_is_none(db, fake_crontab):
    db.add(
        CronJob(
            site_id=None,
            minute="*", hour="*", day_of_month="*", month="*", day_of_week="*",
            command="echo server",
        )
    )
    db.commit()

    tasks.sync_cron(_FakeCtx({"site_id": None}))

    assert len(fake_crontab) == 1
    args, kwargs = fake_crontab[0]
    assert args == ["crontab", "-u", "root", "-"]
    assert "echo server" in kwargs["input"]


def test_sync_cron_only_pulls_jobs_for_its_own_target(db, site, fake_crontab):
    """A root sync must not pick up a site's jobs, and vice versa -- each
    target's crontab is rebuilt only from the rows that actually belong to
    it."""
    db.add_all(
        [
            CronJob(
                site_id=site.id,
                minute="*", hour="*", day_of_month="*", month="*", day_of_week="*",
                command="echo site-job",
            ),
            CronJob(
                site_id=None,
                minute="*", hour="*", day_of_month="*", month="*", day_of_week="*",
                command="echo server-job",
            ),
        ]
    )
    db.commit()

    tasks.sync_cron(_FakeCtx({"site_id": None}))

    assert len(fake_crontab) == 1
    _args, kwargs = fake_crontab[0]
    assert "echo server-job" in kwargs["input"]
    assert "echo site-job" not in kwargs["input"]


def test_sync_cron_fails_the_job_when_the_site_no_longer_exists(db):
    from app.jobs import JobFailed

    with pytest.raises(JobFailed):
        tasks.sync_cron(_FakeCtx({"site_id": 999}))
