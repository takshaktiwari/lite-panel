"""Node.js / Python application sites: validation, systemd + nginx rendering,
process control and the web routes.

Nothing here touches systemd, apt or a real nginx -- those are faked, the same
split the rest of the suite uses.  What is verified for real is everything the
panel *generates*: the unit file, the environment file and the vhost.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import jobs
from app.main import create_app
from app.models import Job, Site
from app.providers.python import PythonProvider
from app.services import apps as apps_service
from app.services import logs as logs_service
from app.services import renderer
from app.services import sites as sites_service
from app.validators import (
    ValidationError,
    parse_app_env,
    validate_app_command,
    validate_app_env,
    validate_app_port,
    validate_app_type,
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
        id=1,
        name="shop",
        domain="shop.example.com",
        system_user="site_shop",
        root_dir="/var/www/shop",
        webroot="/var/www/shop/app",
        php_version=None,
        ssl_enabled=False,
        redirect_www=True,
        upload_limit_mb=64,
        app_type="node",
        app_command="npm start",
        app_port=3001,
        app_env="",
        aliases=[],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


# --------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------


def test_app_type_defaults_to_web_and_rejects_unknown():
    assert validate_app_type("") == "web"
    assert validate_app_type("Node") == "node"
    with pytest.raises(ValidationError):
        validate_app_type("ruby")


def test_app_command_is_required_single_line_and_bounded():
    assert validate_app_command("  npm start ") == "npm start"
    for bad in ("", "a\nb", "x" * 501):
        with pytest.raises(ValidationError):
            validate_app_command(bad)


@pytest.mark.parametrize("port", ["80", "1023", "70000", "abc", "8765", "3306"])
def test_app_port_rejects_privileged_reserved_and_garbage(port):
    with pytest.raises(ValidationError):
        validate_app_port(port)


def test_app_port_accepts_a_normal_port():
    assert validate_app_port("3005") == 3005


def test_env_parsing_handles_quotes_comments_and_export():
    text = 'A=1\n# comment\n\nexport B="two words"\nC=\nD=\'x\'\n'
    assert parse_app_env(text) == [("A", "1"), ("B", "two words"), ("C", ""), ("D", "x")]


@pytest.mark.parametrize(
    "bad", ["noequals", "1BAD=x", "PORT=9", "HOST=0.0.0.0", "A=1\nA=2", "bad key=1"]
)
def test_env_parsing_rejects_malformed_managed_and_duplicate_lines(bad):
    with pytest.raises(ValidationError):
        parse_app_env(bad)


def test_validate_app_env_normalises():
    assert validate_app_env('export A="1"\n\nB=2') == "A=1\nB=2"


# --------------------------------------------------------------------------
# systemd unit + environment file
# --------------------------------------------------------------------------


def test_systemd_quote_escapes_what_systemd_would_interpret():
    assert apps_service.systemd_quote('echo "hi" %n $PORT \\') == 'echo \\"hi\\" %%n $$PORT \\\\'


def test_unit_runs_as_the_site_user_in_the_app_folder():
    unit = apps_service.render_unit(make_site())
    assert "User=site_shop" in unit
    assert "Group=site_shop" in unit
    assert "WorkingDirectory=/var/www/shop/app" in unit
    assert 'Environment="PORT=3001"' in unit
    assert 'Environment="HOST=127.0.0.1"' in unit
    assert 'Environment="NODE_ENV=production"' in unit
    assert 'ExecStart=/bin/bash -c "npm start"' in unit
    assert "Restart=always" in unit
    assert "NoNewPrivileges=true" in unit
    assert "WantedBy=multi-user.target" in unit


def test_node_path_includes_local_node_modules_bin():
    unit = apps_service.render_unit(make_site())
    assert 'Environment="PATH=/var/www/shop/app/node_modules/.bin:' in unit


def test_python_unit_puts_the_virtualenv_first_on_path():
    site = make_site(app_type="python", app_command="gunicorn app:app --bind 127.0.0.1:{PORT}")
    unit = apps_service.render_unit(site)
    assert 'Environment="PATH=/var/www/shop/app/.venv/bin:' in unit
    assert 'Environment="VIRTUAL_ENV=/var/www/shop/app/.venv"' in unit
    assert 'Environment="PYTHONUNBUFFERED=1"' in unit
    assert "NODE_ENV" not in unit


def test_port_placeholder_is_substituted_in_the_command():
    site = make_site(app_type="python", app_command="uvicorn main:app --port {PORT}", app_port=3010)
    assert "uvicorn main:app --port 3010" in apps_service.render_unit(site)
    assert "{PORT}" not in apps_service.render_unit(site)


def test_blank_command_falls_back_to_the_runtime_default():
    site = make_site(app_command=None)
    assert 'ExecStart=/bin/bash -c "npm start"' in apps_service.render_unit(site)


def test_shell_metacharacters_in_the_command_survive_quoting():
    site = make_site(app_command='cd src && node "server.js" --name=%h $HOME')
    unit = apps_service.render_unit(site)
    assert 'ExecStart=/bin/bash -c "cd src && node \\"server.js\\" --name=%%h $$HOME"' in unit


def test_env_file_escapes_quotes_and_is_referenced_by_the_unit():
    site = make_site(app_env='SECRET=a"b\\c\nDEBUG=0')
    text = apps_service.render_env_file(site)
    assert 'SECRET="a\\"b\\\\c"' in text
    assert 'DEBUG="0"' in text
    assert "EnvironmentFile=-" in apps_service.render_unit(site)


@pytest.fixture
def fake_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(apps_service, "UNIT_DIR", tmp_path / "units")
    monkeypatch.setattr(apps_service, "env_dir", lambda: tmp_path / "apps")
    calls = []
    monkeypatch.setattr(apps_service, "_systemctl", lambda args, **kw: calls.append(args))
    return tmp_path, calls


def test_render_files_writes_a_private_env_file_and_reloads_systemd_once(fake_dirs):
    tmp_path, calls = fake_dirs
    site = make_site(app_env="TOKEN=abc")

    assert apps_service.render_files(site) is True
    unit = tmp_path / "units" / "lite-panel-app-shop.service"
    env = tmp_path / "apps" / "shop.env"
    assert unit.exists() and env.exists()
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert 'TOKEN="abc"' in env.read_text()
    assert calls == [["daemon-reload"]]

    # Idempotent: an unchanged unit doesn't trigger another reload.
    assert apps_service.render_files(site) is False
    assert calls == [["daemon-reload"]]


def test_remove_files_deletes_both_and_reloads(fake_dirs):
    tmp_path, calls = fake_dirs
    site = make_site()
    apps_service.render_files(site)
    calls.clear()

    apps_service.remove_files("shop")
    assert not (tmp_path / "units" / "lite-panel-app-shop.service").exists()
    assert not (tmp_path / "apps" / "shop.env").exists()
    assert calls == [["daemon-reload"]]


# --------------------------------------------------------------------------
# nginx vhost
# --------------------------------------------------------------------------


def _vhost(site, **extra):
    context = {
        "site": site,
        "server_names": [site.domain],
        "fpm_socket": None,
        "acme_root": "/var/www/lite-panel-acme",
        "www_redirect_only": True,
        "www_https_redirect": False,
    }
    context.update(extra)
    return renderer.render("vhost.conf.j2", context)


def test_app_vhost_proxies_to_the_app_port_with_websocket_support():
    out = _vhost(make_site(name="my-shop"), app_port=3001, app_label="Node.js")
    assert "proxy_pass http://127.0.0.1:3001;" in out
    assert "proxy_set_header Upgrade $http_upgrade;" in out
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in out
    # Hyphens aren't legal in nginx variable names; the per-site map uses underscores.
    assert "map $http_upgrade $lp_connection_my_shop {" in out
    assert "proxy_set_header Connection $lp_connection_my_shop;" in out
    assert "include /etc/nginx/custom/lite-panel-my-shop-custom.conf;" in out
    # No PHP / static serving for an app.
    assert "fastcgi" not in out
    assert "try_files" not in out
    assert "root /var/www/shop/app;" not in out


def test_app_vhost_still_serves_the_acme_challenge_and_ssl_block():
    site = make_site(ssl_enabled=True)
    out = _vhost(site, app_port=3001, app_label="Node.js", www_redirect_only=True)
    assert "/.well-known/acme-challenge/" in out
    assert "listen 443 ssl http2;" in out
    assert out.count("proxy_pass http://127.0.0.1:3001;") == 1


def test_non_app_vhost_is_unchanged_without_an_app_port():
    out = _vhost(make_site(app_type="web"), fpm_socket="/run/php/x.sock")
    assert "proxy_pass" not in out
    assert "fastcgi_pass unix:/run/php/x.sock;" in out
    assert "map $http_upgrade" not in out


# --------------------------------------------------------------------------
# Ports
# --------------------------------------------------------------------------


def _db_site(db, name, port):
    site = Site(
        name=name, domain=f"{name}.example", system_user=f"site_{name}",
        root_dir=f"/var/www/{name}", webroot=f"/var/www/{name}",
        app_type="node", app_port=port,
    )
    db.add(site)
    db.commit()
    return site


def test_allocate_port_skips_ports_already_given_to_sites(db, monkeypatch):
    monkeypatch.setattr(apps_service, "port_in_use_locally", lambda port: False)
    _db_site(db, "a", 3001)
    _db_site(db, "b", 3002)
    assert apps_service.allocate_port(db) == 3003


def test_allocate_port_skips_ports_something_is_listening_on(db, monkeypatch):
    monkeypatch.setattr(apps_service, "port_in_use_locally", lambda port: port == 3001)
    assert apps_service.allocate_port(db) == 3002


def test_check_port_free_rejects_another_sites_port_but_allows_its_own(db):
    site = _db_site(db, "a", 3001)
    with pytest.raises(ValidationError):
        apps_service.check_port_free(db, 3001)
    assert apps_service.check_port_free(db, 3001, site=site) == 3001


# --------------------------------------------------------------------------
# Process control
# --------------------------------------------------------------------------


@pytest.fixture
def control_env(monkeypatch, fake_dirs):
    monkeypatch.setattr(apps_service, "require_runtime", lambda app_type: None)
    monkeypatch.setattr(apps_service.time, "sleep", lambda s: None)
    monkeypatch.setattr(apps_service.logs_service, "tail_journal", lambda unit, n: "boom: ModuleNotFoundError")
    return fake_dirs


def _state(active, detail=""):
    return SimpleNamespace(active=active, installed=True, detail=detail or ("active" if active else "failed"))


def test_start_enables_the_unit_and_confirms_it_stayed_up(control_env, monkeypatch):
    ctx = FakeCtx()
    monkeypatch.setattr(apps_service, "state", lambda site: _state(True))
    apps_service.control(ctx, make_site(), "start")
    assert (["systemctl", "enable", "--now", "lite-panel-app-shop.service"], {"timeout": 60}) in ctx.commands
    assert any("is running on 127.0.0.1:3001" in line for line in ctx.lines)


def test_start_fails_with_the_log_tail_when_the_app_crashes(control_env, monkeypatch):
    ctx = FakeCtx()
    monkeypatch.setattr(apps_service, "state", lambda site: _state(False, "activating"))
    with pytest.raises(RuntimeError, match="exited right after starting"):
        apps_service.control(ctx, make_site(), "start")
    assert any("ModuleNotFoundError" in line for line in ctx.lines)


def test_stop_disables_so_the_app_stays_off_after_reboot(control_env):
    ctx = FakeCtx()
    apps_service.control(ctx, make_site(), "stop")
    assert (["systemctl", "disable", "--now", "lite-panel-app-shop.service"], {"timeout": 60}) in ctx.commands


def test_restart_enables_then_restarts(control_env, monkeypatch):
    ctx = FakeCtx()
    monkeypatch.setattr(apps_service, "state", lambda site: _state(True))
    apps_service.control(ctx, make_site(), "restart")
    verbs = [c[0][1] for c in ctx.commands]
    assert verbs == ["enable", "restart"]


def test_unknown_action_is_rejected(control_env):
    with pytest.raises(ValidationError):
        apps_service.control(FakeCtx(), make_site(), "reload")


def test_control_without_an_installed_runtime_refuses_to_start(monkeypatch, fake_dirs):
    monkeypatch.setattr(
        apps_service, "get_provider", lambda key: MagicMock(is_installed=lambda: False)
    )
    with pytest.raises(ValidationError, match="isn't installed"):
        apps_service.control(FakeCtx(), make_site(), "start")


def test_teardown_stops_and_removes_the_unit(fake_dirs):
    tmp_path, _calls = fake_dirs
    site = make_site()
    apps_service.render_files(site)
    monkey_ctx = FakeCtx()
    apps_service.teardown(monkey_ctx, site)
    assert not (tmp_path / "units" / "lite-panel-app-shop.service").exists()


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


@pytest.fixture
def runtime_ok(monkeypatch):
    monkeypatch.setattr(apps_service, "require_runtime", lambda app_type: None)


def test_node_install_uses_npm_ci_with_a_lockfile_and_runs_as_the_site_user(tmp_path, runtime_ok):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "package-lock.json").write_text("{}")
    site = make_site(webroot=str(tmp_path))
    ctx = FakeCtx()

    apps_service.install_dependencies(ctx, site, build=True)

    first, kwargs = ctx.commands[0]
    assert first[:5] == ["runuser", "-u", "site_shop", "--", "env"]
    assert first[-2:] == ["npm", "ci"]
    assert kwargs["cwd"] == tmp_path
    assert ctx.commands[1][0][-3:] == ["npm", "run", "build"]


def test_node_install_without_a_lockfile_uses_npm_install(tmp_path, runtime_ok):
    (tmp_path / "package.json").write_text("{}")
    ctx = FakeCtx()
    apps_service.install_dependencies(ctx, make_site(webroot=str(tmp_path)))
    assert ctx.commands[0][0][-2:] == ["npm", "install"]
    assert len(ctx.commands) == 1


def test_node_install_requires_a_package_json(tmp_path, runtime_ok):
    with pytest.raises(ValidationError, match="package.json"):
        apps_service.install_dependencies(FakeCtx(), make_site(webroot=str(tmp_path)))


def test_python_install_creates_the_venv_then_installs_requirements(tmp_path, runtime_ok):
    (tmp_path / "requirements.txt").write_text("flask\n")
    site = make_site(app_type="python", webroot=str(tmp_path))
    ctx = FakeCtx()

    apps_service.install_dependencies(ctx, site)

    venv = str(tmp_path / ".venv")
    assert ctx.commands[0][0][-4:] == ["python3", "-m", "venv", venv]
    assert ctx.commands[1][0][-4:] == [venv + "/bin/pip", "install", "-r", "requirements.txt"]


def test_python_install_reuses_an_existing_venv(tmp_path, runtime_ok):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    (tmp_path / "requirements.txt").write_text("flask\n")
    ctx = FakeCtx()
    apps_service.install_dependencies(ctx, make_site(app_type="python", webroot=str(tmp_path)))
    assert len(ctx.commands) == 1
    assert "pip" in ctx.commands[0][0][-4]


def test_python_install_without_requirements_still_prepares_the_venv(tmp_path, runtime_ok):
    ctx = FakeCtx()
    apps_service.install_dependencies(ctx, make_site(app_type="python", webroot=str(tmp_path)))
    assert len(ctx.commands) == 1
    assert any("No requirements.txt" in line for line in ctx.lines)


def test_install_refuses_non_app_sites(runtime_ok):
    with pytest.raises(ValidationError):
        apps_service.install_dependencies(FakeCtx(), make_site(app_type="web"))


# --------------------------------------------------------------------------
# Site creation / deletion
# --------------------------------------------------------------------------


@pytest.fixture
def fake_site_system(monkeypatch, tmp_path):
    monkeypatch.setattr(sites_service, "sites_root", lambda: tmp_path)
    monkeypatch.setattr(sites_service, "_create_system_user", lambda ctx, u, h: None)
    created = []
    monkeypatch.setattr(sites_service, "_create_directories", lambda ctx, site: created.append(site))
    monkeypatch.setattr(sites_service, "render_site", lambda site: None)
    monkeypatch.setattr(sites_service, "_reload_services", lambda ctx, site: None)
    monkeypatch.setattr(sites_service, "_write_placeholder_page", lambda site: pytest.fail("placeholder written"))
    monkeypatch.setattr(apps_service, "require_runtime", lambda app_type: None)
    monkeypatch.setattr(apps_service, "port_in_use_locally", lambda port: False)
    return created


def test_create_node_site_stores_command_port_and_env(db, fake_site_system):
    site = sites_service.create_site(
        db, FakeCtx(), name="shop", domain="shop.example.com", php_version="8.3",
        app_type="node", app_env='A="1"',
    )
    assert site.app_type == "node"
    assert site.app_command == "npm start"
    assert site.app_port == 3001
    assert site.app_env == "A=1"
    # PHP is meaningless for an app and is dropped.
    assert site.php_version is None


def test_create_python_site_uses_the_python_default_command(db, fake_site_system):
    site = sites_service.create_site(
        db, FakeCtx(), name="api", domain="api.example.com", app_type="python"
    )
    assert site.app_command.startswith("gunicorn")


def test_two_app_sites_get_different_ports(db, fake_site_system):
    a = sites_service.create_site(db, FakeCtx(), name="a", domain="a.example.com", app_type="node")
    b = sites_service.create_site(db, FakeCtx(), name="b", domain="b.example.com", app_type="node")
    assert a.app_port != b.app_port


def test_create_app_site_rejects_a_taken_port(db, fake_site_system):
    sites_service.create_site(db, FakeCtx(), name="a", domain="a.example.com", app_type="node", app_port=3500)
    with pytest.raises(ValidationError, match="already used"):
        sites_service.create_site(db, FakeCtx(), name="b", domain="b.example.com", app_type="node", app_port=3500)


def test_create_app_site_requires_the_runtime(db, fake_site_system, monkeypatch):
    def refuse(app_type):
        raise ValidationError("Node.js isn't installed yet.")

    monkeypatch.setattr(apps_service, "require_runtime", refuse)
    with pytest.raises(ValidationError, match="isn't installed"):
        sites_service.create_site(db, FakeCtx(), name="a", domain="a.example.com", app_type="node")


def test_plain_site_creation_is_unchanged(db, fake_site_system, monkeypatch):
    monkeypatch.setattr(sites_service, "_write_placeholder_page", lambda site: None)
    site = sites_service.create_site(db, FakeCtx(), name="plain", domain="plain.example.com")
    assert site.app_type == "web"
    assert site.app_command is None and site.app_port is None


def test_php_version_change_is_refused_for_app_sites(db):
    site = _db_site(db, "a", 3001)
    with pytest.raises(ValidationError, match="PHP"):
        sites_service.set_php_version(db, FakeCtx(), site, "8.3")


def test_delete_app_site_tears_down_its_service(db, monkeypatch, tmp_path):
    fake_provider = MagicMock()
    fake_provider.installed_versions.return_value = []
    monkeypatch.setattr(sites_service, "get_provider", lambda key: fake_provider)
    monkeypatch.setattr(sites_service.cron_service, "remove_crontab", lambda u: None)
    monkeypatch.setattr(sites_service, "_remove_from_ftp_userlist", lambda u: None)
    torn = []
    monkeypatch.setattr(apps_service, "teardown", lambda ctx, site: torn.append(site.name))

    site = _db_site(db, "a", 3001)
    sites_service.delete_site(db, FakeCtx(), site)
    assert torn == ["a"]


# --------------------------------------------------------------------------
# Logs + provider
# --------------------------------------------------------------------------


def test_only_well_formed_app_units_are_readable_from_the_journal():
    assert apps_service.is_app_unit("lite-panel-app-shop.service")
    for bad in ("sshd.service", "lite-panel-app-.service", "lite-panel-app-a;rm.service", "../x"):
        assert not apps_service.is_app_unit(bad)
        with pytest.raises(ValidationError):
            logs_service.tail_journal(bad)


def test_app_unit_journal_is_allowed(monkeypatch):
    monkeypatch.setattr(logs_service.shutil, "which", lambda name: None)
    assert "not available" in logs_service.tail_journal("lite-panel-app-shop.service")


def test_python_provider_reports_installed_by_venv_package(monkeypatch):
    monkeypatch.setattr("app.providers.python.apt.installed_version", lambda pkg: "3.12.3-0" if pkg == "python3-venv" else None)
    assert PythonProvider().is_installed()
    monkeypatch.setattr("app.providers.python.apt.installed_version", lambda pkg: None)
    assert not PythonProvider().is_installed()


def test_python_uninstall_never_removes_the_interpreter(monkeypatch):
    removed = []
    monkeypatch.setattr("app.providers.python.apt.remove", lambda ctx, pkgs, **kw: removed.extend(pkgs))
    PythonProvider().uninstall(FakeCtx())
    assert "python3" not in removed
    assert "python3-venv" in removed


# --------------------------------------------------------------------------
# Web routes
# --------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def web(monkeypatch, db, admin):
    monkeypatch.setattr(jobs.worker, "submit", lambda job_id: None)
    fake = MagicMock()
    fake.is_installed.return_value = True
    fake.installed_versions.return_value = []
    monkeypatch.setattr("app.routers.sites.get_provider", lambda key: fake)
    monkeypatch.setattr(apps_service, "get_provider", lambda key: fake)
    monkeypatch.setattr(apps_service, "port_in_use_locally", lambda port: False)
    monkeypatch.setattr(apps_service, "state", lambda site: _state(True))
    app = create_app()
    with TestClient(app, follow_redirects=False) as client:
        response = client.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
        assert response.status_code == 303
        yield client


def _csrf(client, path="/sites/new"):
    page = client.get(path)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


def _last_job(db):
    return db.scalars(select(Job).order_by(Job.id.desc())).first()


def test_new_site_form_offers_node_and_python(web):
    page = web.get("/sites/new")
    assert page.status_code == 200
    assert 'value="node"' in page.text
    assert 'value="python"' in page.text
    assert "npm start" in page.text
    assert "gunicorn app:app" in page.text
    assert "/static/site-type.js" in page.text


def test_creating_a_node_site_enqueues_the_app_fields(web, db):
    response = web.post("/sites/new", data={
        "csrf_token": _csrf(web), "domain": "shop.example.com", "app_type": "node",
        "app_command": "node server.js", "app_port": "3050", "app_env": "A=1\nB=2",
        "php_version": "8.3",
    })
    assert response.status_code == 303
    payload = json.loads(_last_job(db).payload)
    assert payload["app_type"] == "node"
    assert payload["app_command"] == "node server.js"
    assert payload["app_port"] == 3050
    assert payload["app_env"] == "A=1\nB=2"
    assert payload["php_version"] is None


def test_creating_a_python_site_without_a_command_leaves_it_to_the_default(web, db):
    web.post("/sites/new", data={
        "csrf_token": _csrf(web), "domain": "api.example.com", "app_type": "python",
    })
    payload = json.loads(_last_job(db).payload)
    assert payload["app_type"] == "python"
    assert payload["app_command"].startswith("gunicorn")
    assert payload["app_port"] is None


def test_bad_env_is_reported_on_the_form(web):
    response = web.post("/sites/new", data={
        "csrf_token": _csrf(web), "domain": "shop.example.com", "app_type": "node",
        "app_env": "PORT=1",
    })
    assert response.status_code == 400
    assert "set by the panel" in response.text


def test_a_plain_site_still_enqueues_as_web(web, db):
    web.post("/sites/new", data={"csrf_token": _csrf(web), "domain": "plain.example.com"})
    payload = json.loads(_last_job(db).payload)
    assert payload["app_type"] == "web"
    assert payload["app_command"] is None


def test_runtime_not_installed_blocks_creation(web, monkeypatch):
    def refuse(app_type):
        raise ValidationError("Node.js isn't installed yet. Install it from the Stack page first.")

    monkeypatch.setattr(apps_service, "require_runtime", refuse)
    response = web.post("/sites/new", data={
        "csrf_token": _csrf(web), "domain": "shop.example.com", "app_type": "node",
    })
    assert response.status_code == 400
    assert "installed yet" in response.text


def test_site_list_shows_the_app_type(web, db):
    _db_site(db, "shop", 3001)
    assert "Node.js" in web.get("/sites").text


def test_detail_page_shows_app_controls_instead_of_php(web, db):
    site = _db_site(db, "shop", 3001)
    page = web.get(f"/sites/{site.id}")
    assert page.status_code == 200
    assert "Node.js application" in page.text
    assert "127.0.0.1:3001" in page.text
    assert "Install dependencies" in page.text
    assert "Application settings" in page.text
    assert "/static/site-app.js" in page.text
    assert "<h2>PHP version</h2>" not in page.text
    assert "running" in page.text


def test_detail_page_for_a_plain_site_is_unchanged(web, db):
    site = Site(name="plain", domain="plain.example", system_user="site_plain",
                root_dir="/var/www/plain", webroot="/var/www/plain")
    db.add(site)
    db.commit()
    page = web.get(f"/sites/{site.id}")
    assert "<h2>PHP version</h2>" in page.text
    assert "Application settings" not in page.text


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_control_enqueues_the_action(web, db, action):
    site = _db_site(db, "shop", 3001)
    response = web.post(f"/sites/{site.id}/app/control", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), "action": action,
    })
    assert response.status_code == 303
    job = _last_job(db)
    assert job.kind == "site.app_control"
    assert json.loads(job.payload) == {"site_id": site.id, "action": action}


def test_control_rejects_unknown_actions(web, db):
    site = _db_site(db, "shop", 3001)
    response = web.post(f"/sites/{site.id}/app/control", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), "action": "explode",
    })
    assert "error=" in response.headers["location"]
    assert _last_job(db) is None


def test_control_requires_csrf(web, db):
    site = _db_site(db, "shop", 3001)
    assert web.post(f"/sites/{site.id}/app/control", data={"action": "start"}).status_code == 400


def test_control_is_refused_for_plain_sites(web, db):
    site = Site(name="plain", domain="plain.example", system_user="site_plain",
                root_dir="/var/www/plain", webroot="/var/www/plain")
    db.add(site)
    db.commit()
    response = web.post(f"/sites/{site.id}/app/control", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), "action": "start",
    })
    assert "error=" in response.headers["location"]
    assert _last_job(db) is None


def test_install_enqueues_with_the_build_flag(web, db):
    site = _db_site(db, "shop", 3001)
    web.post(f"/sites/{site.id}/app/install", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), "build": "1",
    })
    job = _last_job(db)
    assert job.kind == "site.app_install"
    assert json.loads(job.payload) == {"site_id": site.id, "build": True}


def test_settings_validate_then_enqueue(web, db):
    site = _db_site(db, "shop", 3001)
    token = _csrf(web, f"/sites/{site.id}")
    web.post(f"/sites/{site.id}/app/settings", data={
        "csrf_token": token, "command": "npm run serve", "port": "3077", "env": "export K=v",
    })
    job = _last_job(db)
    assert job.kind == "site.app_settings"
    assert json.loads(job.payload) == {
        "site_id": site.id, "command": "npm run serve", "port": 3077, "env": "K=v",
    }


@pytest.mark.parametrize("data", [
    {"command": "", "port": "3001", "env": ""},
    {"command": "x", "port": "80", "env": ""},
    {"command": "x", "port": "3001", "env": "PORT=1"},
])
def test_settings_reject_bad_input_without_queueing(web, db, data):
    site = _db_site(db, "shop", 3001)
    response = web.post(f"/sites/{site.id}/app/settings", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), **data,
    })
    assert "error=" in response.headers["location"]
    assert _last_job(db) is None


def test_settings_reject_a_port_owned_by_another_site(web, db):
    _db_site(db, "other", 3002)
    site = _db_site(db, "shop", 3001)
    response = web.post(f"/sites/{site.id}/app/settings", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), "command": "x", "port": "3002", "env": "",
    })
    assert "already" in response.headers["location"]


def test_status_and_logs_endpoints(web, db, monkeypatch):
    site = _db_site(db, "shop", 3001)
    status = web.get(f"/sites/{site.id}/app/status").json()
    assert status["active"] is True
    monkeypatch.setattr(apps_service.logs_service, "tail_journal", lambda unit, n: f"log for {unit}")
    assert web.get(f"/sites/{site.id}/app/logs").json() == {"logs": "log for lite-panel-app-shop.service"}


def test_status_for_a_plain_site_is_404(web, db):
    site = Site(name="plain", domain="plain.example", system_user="site_plain",
                root_dir="/var/www/plain", webroot="/var/www/plain")
    db.add(site)
    db.commit()
    assert web.get(f"/sites/{site.id}/app/status").status_code == 404


def test_php_change_is_refused_for_app_sites(web, db):
    site = _db_site(db, "shop", 3001)
    response = web.post(f"/sites/{site.id}/php", data={
        "csrf_token": _csrf(web, f"/sites/{site.id}"), "php_version": "8.3",
    })
    assert "error=" in response.headers["location"]
    assert _last_job(db) is None
