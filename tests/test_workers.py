"""Site background workers: validation, systemd template-unit rendering,
process control, the job-facing service calls and the web routes.

systemd itself is faked; what is verified for real is everything the panel
*generates* (unit text, unit names) and the commands it would issue.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import jobs
from app.main import create_app
from app.models import Job, Site, SiteWorker
from app.services import apps as apps_service
from app.services import logs as logs_service
from app.services import sites as sites_service
from app.services import workers as workers_service
from app.validators import (
    ValidationError,
    validate_stop_timeout,
    validate_worker_command,
    validate_worker_name,
    validate_worker_processes,
)


class FakeCtx:
    def __init__(self):
        self.lines = []
        self.commands = []

    def log(self, line):
        self.lines.append(line)

    def run(self, args, **kwargs):
        self.commands.append((list(args), kwargs))
        return 0

    def check(self, args, **kwargs):
        self.commands.append((list(args), kwargs))


def make_site(**overrides):
    values = dict(
        id=1, name="shop", domain="shop.example.com", system_user="site_shop",
        root_dir="/var/www/shop", webroot="/var/www/shop/public", php_version="8.3",
        app_type="web", app_command=None, app_port=None, app_env="", workers=[],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def make_worker(**overrides):
    values = dict(
        id=1, site_id=1, name="queue", processes=2, stop_timeout=60,
        command="{PHP} artisan queue:work --tries=3",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def php_cli(monkeypatch):
    fake = MagicMock()
    fake.cli_path_for.side_effect = lambda v: f"/usr/bin/php{v}"
    monkeypatch.setattr(workers_service, "get_provider", lambda key: fake)


# --------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------


def test_worker_name_rules():
    assert validate_worker_name(" Queue ") == "queue"
    assert validate_worker_name("q") == "q"
    assert validate_worker_name("my-worker-2") == "my-worker-2"
    for bad in ("", "1queue", "queue-", "has_underscore", "a b", "x" * 25, "a.b", "../x"):
        with pytest.raises(ValidationError):
            validate_worker_name(bad)


def test_worker_command_is_required_single_line_and_bounded():
    assert validate_worker_command(" php artisan x ") == "php artisan x"
    for bad in ("", "a\nb", "x" * 501):
        with pytest.raises(ValidationError):
            validate_worker_command(bad)


def test_processes_and_timeout_ranges():
    assert validate_worker_processes("3") == 3
    assert validate_stop_timeout("90") == 90
    for bad in ("0", "9", "abc", ""):
        with pytest.raises(ValidationError):
            validate_worker_processes(bad)
    for bad in ("4", "3601", "x"):
        with pytest.raises(ValidationError):
            validate_stop_timeout(bad)


# --------------------------------------------------------------------------
# Names and units
# --------------------------------------------------------------------------


def test_unit_names_cannot_collide_between_sites_and_workers():
    assert workers_service.template_name("a-b", "c") != workers_service.template_name("a", "b-c")
    assert workers_service.instance_name("shop", "queue", 2) == "lite-panel-worker-shop.queue@2.service"
    assert workers_service.instance_names("shop", "queue", 2) == [
        "lite-panel-worker-shop.queue@1.service",
        "lite-panel-worker-shop.queue@2.service",
    ]


def test_php_worker_unit_runs_in_the_project_root_with_the_site_php():
    unit = workers_service.render_unit(make_site(), make_worker())
    assert "User=site_shop" in unit
    assert "WorkingDirectory=/var/www/shop\n" in unit  # parent of public/
    assert 'ExecStart=/bin/bash -c "/usr/bin/php8.3 artisan queue:work --tries=3"' in unit
    assert "TimeoutStopSec=60" in unit
    assert "Restart=always" in unit
    assert "SyslogIdentifier=lite-panel-worker-shop.queue" in unit
    assert "NoNewPrivileges=true" in unit
    assert "EnvironmentFile" not in unit
    assert "{PHP}" not in unit


def test_webroot_that_is_not_public_is_used_as_is():
    unit = workers_service.render_unit(make_site(webroot="/var/www/shop/app"), make_worker())
    assert "WorkingDirectory=/var/www/shop/app\n" in unit


def test_app_site_worker_gets_the_runtime_path_and_env_file(monkeypatch):
    site = make_site(app_type="python", php_version=None, webroot="/var/www/shop/app")
    unit = workers_service.render_unit(site, make_worker(command="celery -A app worker"))
    assert 'Environment="PATH=/var/www/shop/app/.venv/bin:' in unit
    assert "EnvironmentFile=-" in unit
    assert "WorkingDirectory=/var/www/shop/app\n" in unit


def test_php_placeholder_without_a_php_version_is_refused():
    with pytest.raises(ValidationError, match="no PHP"):
        workers_service.render_unit(make_site(php_version=None), make_worker())


def test_shell_metacharacters_survive_systemd_quoting():
    unit = workers_service.render_unit(
        make_site(), make_worker(command='node "a.js" --n=%h $HOME')
    )
    assert 'ExecStart=/bin/bash -c "node \\"a.js\\" --n=%%h $$HOME"' in unit


# --------------------------------------------------------------------------
# Writing unit files
# --------------------------------------------------------------------------


@pytest.fixture
def fake_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(workers_service, "UNIT_DIR", tmp_path / "units")
    calls = []
    monkeypatch.setattr(apps_service, "_systemctl", lambda args, **kw: calls.append(args))
    return tmp_path, calls


def test_render_files_writes_a_template_unit_per_worker_and_reloads_once(fake_dirs):
    tmp_path, calls = fake_dirs
    site = make_site(workers=[make_worker(), make_worker(id=2, name="horizon", command="{PHP} artisan horizon")])

    assert workers_service.render_files(site) is True
    assert (tmp_path / "units" / "lite-panel-worker-shop.queue@.service").exists()
    assert (tmp_path / "units" / "lite-panel-worker-shop.horizon@.service").exists()
    assert calls == [["daemon-reload"]]

    assert workers_service.render_files(site) is False
    assert calls == [["daemon-reload"]]


def test_render_files_skips_a_worker_it_cannot_render(fake_dirs):
    tmp_path, calls = fake_dirs
    site = make_site(php_version=None, workers=[make_worker()])
    assert workers_service.render_files(site) is False
    assert not (tmp_path / "units").exists()


def test_site_without_workers_renders_nothing(fake_dirs):
    tmp_path, calls = fake_dirs
    assert workers_service.render_files(make_site()) is False
    assert calls == []


# --------------------------------------------------------------------------
# Control
# --------------------------------------------------------------------------


@pytest.fixture
def control_env(monkeypatch, fake_dirs):
    monkeypatch.setattr(workers_service.time, "sleep", lambda s: None)
    monkeypatch.setattr(workers_service, "recent_logs", lambda *a, **k: "boom: no such file")
    return fake_dirs


def _state(running, total=2):
    return workers_service.WorkerState(running=running, total=total)


def test_start_enables_every_instance_and_verifies(control_env, monkeypatch):
    monkeypatch.setattr(workers_service, "state", lambda site, worker: _state(2))
    ctx = FakeCtx()
    site = make_site()
    workers_service.control(ctx, site, make_worker(), "start")
    cmd = ctx.commands[0][0]
    assert cmd[:3] == ["systemctl", "enable", "--now"]
    assert cmd[3:] == [
        "lite-panel-worker-shop.queue@1.service",
        "lite-panel-worker-shop.queue@2.service",
    ]
    assert any("2 process(es) running" in line for line in ctx.lines)


def test_start_fails_with_the_log_tail_when_a_process_dies(control_env, monkeypatch):
    monkeypatch.setattr(workers_service, "state", lambda site, worker: _state(1))
    ctx = FakeCtx()
    with pytest.raises(RuntimeError, match="exited right after starting"):
        workers_service.control(ctx, make_site(), make_worker(), "start")
    assert any("boom" in line for line in ctx.lines)


def test_stop_disables_the_instances(control_env):
    ctx = FakeCtx()
    workers_service.control(ctx, make_site(), make_worker(), "stop")
    assert ctx.commands[0][0][:3] == ["systemctl", "disable", "--now"]


def test_restart_enables_then_restarts(control_env, monkeypatch):
    monkeypatch.setattr(workers_service, "state", lambda site, worker: _state(2))
    ctx = FakeCtx()
    workers_service.control(ctx, make_site(), make_worker(), "restart")
    assert [c[0][1] for c in ctx.commands] == ["enable", "restart"]


def test_unknown_action_rejected(control_env):
    with pytest.raises(ValidationError):
        workers_service.control(FakeCtx(), make_site(), make_worker(), "reload")


def test_control_all_needs_workers_and_reports_every_failure(control_env, monkeypatch):
    with pytest.raises(ValidationError, match="no workers"):
        workers_service.control_all(FakeCtx(), make_site(), "restart")

    monkeypatch.setattr(workers_service, "state", lambda site, worker: _state(0))
    site = make_site(workers=[make_worker(), make_worker(id=2, name="horizon")])
    ctx = FakeCtx()
    with pytest.raises(RuntimeError) as exc:
        workers_service.control_all(ctx, site, "start")
    # Both were attempted before failing.
    assert "queue" in str(exc.value) and "horizon" in str(exc.value)


def test_php_change_restarts_only_running_php_workers(monkeypatch):
    site = make_site(workers=[make_worker(), make_worker(id=2, name="node", command="node w.js")])
    monkeypatch.setattr(workers_service, "state", lambda s, w: _state(2))
    ctx = FakeCtx()
    workers_service.restart_php_workers(ctx, site)
    assert len(ctx.commands) == 1
    assert ctx.commands[0][0][:2] == ["systemctl", "restart"]
    assert "queue@1" in ctx.commands[0][0][2]


# --------------------------------------------------------------------------
# Add / update / delete against the database
# --------------------------------------------------------------------------


def _db_site(db, name="shop", php="8.3"):
    site = Site(
        name=name, domain=f"{name}.example", system_user=f"site_{name}",
        root_dir=f"/var/www/{name}", webroot=f"/var/www/{name}/public", php_version=php,
    )
    db.add(site)
    db.commit()
    return site


@pytest.fixture
def svc(monkeypatch, fake_dirs):
    monkeypatch.setattr(workers_service.time, "sleep", lambda s: None)
    monkeypatch.setattr(workers_service, "state", lambda site, worker: _state(worker.processes, worker.processes))
    monkeypatch.setattr(workers_service, "recent_logs", lambda *a, **k: "")
    monkeypatch.setattr(workers_service, "which", lambda name: "/bin/systemctl")
    return fake_dirs


def test_add_worker_persists_renders_and_starts(db, svc):
    tmp_path, calls = svc
    site = _db_site(db)
    ctx = FakeCtx()
    worker = workers_service.add_worker(
        db, ctx, site, name="queue", command="{PHP} artisan queue:work",
        processes=2, stop_timeout=90,
    )
    saved = db.scalars(select(SiteWorker)).all()
    assert [(w.name, w.processes, w.stop_timeout) for w in saved] == [("queue", 2, 90)]
    assert (tmp_path / "units" / "lite-panel-worker-shop.queue@.service").exists()
    assert ctx.commands[0][0][:3] == ["systemctl", "enable", "--now"]
    assert worker.id


def test_add_worker_rejects_duplicates_php_placeholder_without_php_and_the_cap(db, svc):
    site = _db_site(db)
    workers_service.add_worker(db, FakeCtx(), site, name="queue", command="x", processes=1, stop_timeout=60)
    with pytest.raises(ValidationError, match="already has"):
        workers_service.validate_new(site, "queue", "y", 1, 60)

    bare = _db_site(db, "bare", php=None)
    with pytest.raises(ValidationError, match="no PHP"):
        workers_service.validate_new(bare, "q", "{PHP} artisan x", 1, 60)

    for i in range(workers_service.MAX_WORKERS_PER_SITE - 1):
        workers_service.add_worker(
            db, FakeCtx(), site, name=f"w{i}", command="x", processes=1, stop_timeout=60
        )
    with pytest.raises(ValidationError, match="at most"):
        workers_service.validate_new(site, "extra", "x", 1, 60)


def test_update_worker_restarts_when_running_and_stops_dropped_instances(db, svc):
    site = _db_site(db)
    worker = workers_service.add_worker(
        db, FakeCtx(), site, name="queue", command="{PHP} artisan queue:work",
        processes=3, stop_timeout=60,
    )
    ctx = FakeCtx()
    workers_service.update_worker(
        db, ctx, site, worker, command="{PHP} artisan queue:work --tries=5",
        processes=1, stop_timeout=30,
    )
    assert worker.processes == 1 and worker.stop_timeout == 30
    first = ctx.commands[0][0]
    assert first[:3] == ["systemctl", "disable", "--now"]
    assert first[3:] == [
        "lite-panel-worker-shop.queue@2.service",
        "lite-panel-worker-shop.queue@3.service",
    ]
    assert [c[0][1] for c in ctx.commands[1:]] == ["enable", "restart"]


def test_update_worker_that_is_stopped_is_not_started(db, svc, monkeypatch):
    site = _db_site(db)
    worker = workers_service.add_worker(
        db, FakeCtx(), site, name="queue", command="x", processes=1, stop_timeout=60
    )
    monkeypatch.setattr(workers_service, "state", lambda s, w: _state(0, 1))
    ctx = FakeCtx()
    workers_service.update_worker(db, ctx, site, worker, command="y", processes=1, stop_timeout=60)
    assert ctx.commands == []
    assert any("isn't running" in line for line in ctx.lines)


def test_delete_worker_stops_everything_and_removes_unit_and_row(db, svc):
    tmp_path, calls = svc
    site = _db_site(db)
    worker = workers_service.add_worker(
        db, FakeCtx(), site, name="queue", command="x", processes=2, stop_timeout=60
    )
    ctx = FakeCtx()
    workers_service.delete_worker(db, ctx, site, worker)
    assert db.scalars(select(SiteWorker)).all() == []
    assert not (tmp_path / "units" / "lite-panel-worker-shop.queue@.service").exists()
    stop = ctx.commands[0][0]
    assert stop[:3] == ["systemctl", "disable", "--now"]
    assert len(stop) == 3 + 8  # every possible instance, so none is left behind


def test_site_teardown_removes_all_worker_units(db, svc):
    tmp_path, calls = svc
    site = _db_site(db)
    for name in ("queue", "horizon"):
        workers_service.add_worker(db, FakeCtx(), site, name=name, command="x", processes=1, stop_timeout=60)
    db.refresh(site)
    workers_service.teardown(FakeCtx(), site)
    assert list((tmp_path / "units").glob("*.service")) == []


def test_deleting_a_site_tears_down_workers_and_drops_the_rows(db, monkeypatch):
    fake_provider = MagicMock()
    fake_provider.installed_versions.return_value = []
    monkeypatch.setattr(sites_service, "get_provider", lambda key: fake_provider)
    monkeypatch.setattr(sites_service.cron_service, "remove_crontab", lambda u: None)
    monkeypatch.setattr(sites_service, "_remove_from_ftp_userlist", lambda u: None)
    torn = []
    monkeypatch.setattr(workers_service, "teardown", lambda ctx, site: torn.append(site.name))
    site = _db_site(db)
    db.add(SiteWorker(site_id=site.id, name="queue", command="x"))
    db.commit()
    sites_service.delete_site(db, FakeCtx(), site)
    assert torn == ["shop"]
    assert db.scalars(select(SiteWorker)).all() == []


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------


def test_only_well_formed_worker_units_are_readable_from_the_journal(monkeypatch):
    assert workers_service.is_worker_unit("lite-panel-worker-shop.queue@1.service")
    assert workers_service.is_worker_unit("lite-panel-worker-shop.queue@*.service")
    for bad in (
        "lite-panel-worker-shop.service", "lite-panel-worker-a;rm.queue@1.service",
        "lite-panel-worker-shop.queue@x.service", "../x", "sshd.service",
    ):
        assert not workers_service.is_worker_unit(bad)
        with pytest.raises(ValidationError):
            logs_service.tail_journal(bad)

    monkeypatch.setattr(logs_service.shutil, "which", lambda name: None)
    assert "not available" in logs_service.tail_journal("lite-panel-worker-shop.queue@*.service")


# --------------------------------------------------------------------------
# Web routes
# --------------------------------------------------------------------------


@pytest.fixture
def web(monkeypatch, db, admin):
    monkeypatch.setattr(jobs.worker, "submit", lambda job_id: None)
    fake = MagicMock()
    fake.is_installed.return_value = True
    fake.installed_versions.return_value = ["8.3"]
    monkeypatch.setattr("app.routers.sites.get_provider", lambda key: fake)
    monkeypatch.setattr(workers_service, "state", lambda site, worker: _state(worker.processes, worker.processes))
    app = create_app()
    with TestClient(app, follow_redirects=False) as client:
        response = client.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
        assert response.status_code == 303
        yield client


def _csrf(client, path):
    page = client.get(path)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


def _last_job(db):
    return db.scalars(select(Job).order_by(Job.id.desc())).first()


def _with_worker(db, **kw):
    site = _db_site(db)
    worker = SiteWorker(site_id=site.id, name="queue", command="{PHP} artisan queue:work", processes=2, **kw)
    db.add(worker)
    db.commit()
    return site, worker


def test_detail_page_shows_the_workers_panel_and_presets(web, db):
    site, worker = _with_worker(db)
    page = web.get(f"/sites/{site.id}")
    assert page.status_code == 200
    assert "Workers" in page.text
    assert "artisan queue:work" in page.text
    assert "Laravel Horizon" in page.text
    assert "/static/site-workers.js" in page.text
    assert "/docs/workers" in page.text
    assert "2 processes" in page.text


def test_empty_state_and_add_form_for_a_site_without_workers(web, db):
    site = _db_site(db)
    page = web.get(f"/sites/{site.id}")
    assert "No workers yet." in page.text
    assert "Add and start" in page.text
    assert "Restart all" not in page.text


def test_add_enqueues_a_job_with_clean_fields(web, db):
    site = _db_site(db)
    response = web.post(f"/sites/{site.id}/workers/add", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), "name": "Queue",
        "command": "{PHP} artisan queue:work", "processes": "2", "stop_timeout": "90",
    })
    assert response.status_code == 303
    job = _last_job(db)
    assert job.kind == "site.worker_add"
    assert json.loads(job.payload) == {
        "site_id": site.id, "name": "queue", "command": "{PHP} artisan queue:work",
        "processes": 2, "stop_timeout": 90,
    }


@pytest.mark.parametrize("data,message", [
    ({"name": "bad name", "command": "x"}, "Worker name"),
    ({"name": "q", "command": ""}, "command is required"),
    ({"name": "q", "command": "x", "processes": "99"}, "between 1 and"),
    ({"name": "q", "command": "x", "stop_timeout": "1"}, "Stop timeout"),
])
def test_add_validation_errors_are_shown_on_the_site_page(web, db, data, message):
    site = _db_site(db)
    payload = {"csrf_token": _csrf(web, f"/sites/{site.id}"), "processes": "1", "stop_timeout": "60"}
    payload.update(data)
    response = web.post(f"/sites/{site.id}/workers/add", data=payload)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert message.replace(" ", "%20") in response.headers["location"]
    assert _last_job(db) is None


def test_add_duplicate_name_is_rejected(web, db):
    site, _ = _with_worker(db)
    response = web.post(f"/sites/{site.id}/workers/add", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), "name": "queue", "command": "x",
    })
    assert "already%20has" in response.headers["location"]


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_control_enqueues_per_worker(web, db, action):
    site, worker = _with_worker(db)
    web.post(f"/sites/{site.id}/workers/{worker.id}/control", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), "action": action,
    })
    job = _last_job(db)
    assert job.kind == "site.worker_control"
    assert json.loads(job.payload) == {"site_id": site.id, "worker_id": worker.id, "action": action}


def test_control_rejects_unknown_actions_and_foreign_workers(web, db):
    site, worker = _with_worker(db)
    other = _db_site(db, "other")
    token = _csrf(web, f"/sites/{site.id}")
    assert "Unknown" in web.post(
        f"/sites/{site.id}/workers/{worker.id}/control", data={"csrf_token": token, "action": "nuke"}
    ).headers["location"]
    # A worker id belonging to another site must not be reachable through this one.
    assert "no%20longer%20exists" in web.post(
        f"/sites/{other.id}/workers/{worker.id}/control", data={"csrf_token": token, "action": "stop"}
    ).headers["location"]
    assert _last_job(db) is None


def test_restart_all_enqueues_one_job_without_a_worker_id(web, db):
    site, _ = _with_worker(db)
    web.post(f"/sites/{site.id}/workers/restart-all", data={"csrf_token": _csrf(web, f"/sites/{site.id}")})
    job = _last_job(db)
    assert json.loads(job.payload) == {"site_id": site.id, "action": "restart"}


def test_settings_and_delete_enqueue_jobs(web, db):
    site, worker = _with_worker(db)
    token = _csrf(web, f"/sites/{site.id}")
    web.post(f"/sites/{site.id}/workers/{worker.id}/settings", data={
        "csrf_token": token, "command": "{PHP} artisan horizon", "processes": "1", "stop_timeout": "120",
    })
    job = _last_job(db)
    assert job.kind == "site.worker_update"
    assert json.loads(job.payload)["command"] == "{PHP} artisan horizon"

    web.post(f"/sites/{site.id}/workers/{worker.id}/delete", data={"csrf_token": token})
    assert _last_job(db).kind == "site.worker_delete"


def test_status_and_logs_endpoints(web, db, monkeypatch):
    site, worker = _with_worker(db)
    status = web.get(f"/sites/{site.id}/workers/status").json()
    assert status["workers"] == [{"id": worker.id, "active": True, "label": "running"}]

    monkeypatch.setattr(workers_service, "recent_logs", lambda s, w, n: "line one")
    assert web.get(f"/sites/{site.id}/workers/{worker.id}/logs").json() == {"logs": "line one"}
    assert web.get(f"/sites/{site.id}/workers/9999/logs").status_code == 404


def test_routes_require_a_login(db):
    with TestClient(create_app(), follow_redirects=False) as client:
        assert client.get("/sites/1/workers/status").status_code in (303, 401)
        assert client.post("/sites/1/workers/add", data={}).status_code in (303, 401, 403)


def test_worker_jobs_run_the_service(db, monkeypatch):
    from app import tasks

    site = _db_site(db)
    worker = SiteWorker(site_id=site.id, name="queue", command="x")
    db.add(worker)
    db.commit()

    seen = []
    monkeypatch.setattr(workers_service, "control", lambda ctx, s, w, a: seen.append((s.name, w.name, a)))
    monkeypatch.setattr(workers_service, "control_all", lambda ctx, s, a: seen.append((s.name, "*", a)))

    class Ctx(FakeCtx):
        payload = {"site_id": site.id, "worker_id": worker.id, "action": "restart"}

    tasks.control_site_worker(Ctx())
    Ctx.payload = {"site_id": site.id, "action": "stop"}
    tasks.control_site_worker(Ctx())
    assert seen == [("shop", "queue", "restart"), ("shop", "*", "stop")]
