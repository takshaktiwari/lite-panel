"""Letting nginx read a site's files (ACL) without opening them to other sites.

The filesystem is simulated: ``FakeAcl`` understands the handful of getfacl /
setfacl / find invocations the panel issues and keeps the resulting entries, so
the three upgrade situations can be checked end to end -- a fresh site, an old
install that was never fixed, and one that was fixed by hand -- against what
nginx would actually be able to do afterwards.  Real setfacl, nginx and a Linux
kernel are not involved; the isolation property is checked by asserting the
panel only ever names the web server user and never touches the mode bits.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import jobs, tasks
from app.main import create_app
from app.models import Job, Site
from app.services import files as files_service
from app.services import permissions as perms
from app.services import renderer
from app.services import sites as sites_service
from app.shell import CommandResult

WEB = "www-data"


class FakeCtx:
    def __init__(self):
        self.lines = []
        self.commands = []

    def log(self, line):
        self.lines.append(line)

    def run(self, args, **kw):
        self.commands.append(list(args))
        return 0

    def check(self, args, **kw):
        self.commands.append(list(args))

    payload = {}


class FakeAcl:
    """Just enough getfacl / setfacl / find to follow the panel's grants."""

    def __init__(self):
        self.access = {}     # path -> "r-x"
        self.default = {}    # path -> "r-x" (directories only)
        self.writes = []     # every mutating call, in order
        self.other = []      # anything else that ran (usermod ...)

    # -- helpers
    @staticmethod
    def _perms(spec):
        letters = spec.split(":")[-1]
        return ("r" if "r" in letters else "-") + ("w" if "w" in letters else "-") + (
            "x" if ("x" in letters or "X" in letters) else "-"
        )

    def _tree(self, path, *, dirs_only=False):
        out = [path]
        for current, dirnames, filenames in os.walk(path):
            out += [os.path.join(current, d) for d in dirnames]
            if not dirs_only:
                out += [os.path.join(current, f) for f in filenames]
        return out

    def grant_by_hand(self, root):
        """What the operator ran manually: setfacl -R -m ... and -Rd -m ..."""
        for path in self._tree(root):
            self.access[path] = "r-x"
        for path in self._tree(root, dirs_only=True):
            self.default[path] = "r-x"

    # -- the fake `run`
    def run(self, args, **kw):
        args = list(args)
        out = ""
        if args[0] == "getfacl":
            path = args[-1]
            out = "user::rwx\ngroup::r-x\nother::---\n"
            if path in self.access:
                out += f"user:{WEB}:{self.access[path]}\n"
            if path in self.default:
                out += f"default:user:{WEB}:{self.default[path]}\n"
        elif args[0] == "setfacl":
            self._setfacl(args[1:])
        elif args[0] == "find":
            i = args.index("setfacl")
            self._setfacl(["-d", *args[i + 1:-1]] if "-d" not in args[i + 1:] else args[i + 1:-1], base=args[1])
        else:
            self.other.append(args)
        return CommandResult(tuple(args), 0, out, "")

    def _setfacl(self, rest, base=None):
        recursive = "-R" in rest
        default = "-d" in rest
        flags = [a for a in rest if a in ("-m", "-x")]
        mode = flags[0]
        spec = rest[rest.index(mode) + 1]
        path = base or rest[-1]
        self.writes.append((mode, default, recursive, spec, path))
        if base:
            targets = self._tree(base, dirs_only=True)
        elif recursive:
            targets = self._tree(path, dirs_only=default)
        else:
            targets = [path]
        store = self.default if default else self.access
        for target in targets:
            if mode == "-m":
                store[target] = self._perms(spec)
            else:
                store.pop(target, None)

    # -- what nginx could then do
    def can_serve(self, site):
        root, webroot = site.root_dir, site.webroot
        enters = "x" == self.access.get(root, "---")[2] and self.access.get(webroot, "---")[0] == "r"
        return enters


def make_site(tmp_path, name="shop", **overrides):
    root = tmp_path / name
    (root / "public").mkdir(parents=True, exist_ok=True)
    (root / "tmp" / "sessions").mkdir(parents=True, exist_ok=True)
    (root / "public" / "index.html").write_text("<h1>hi</h1>")
    values = dict(
        id=1, name=name, domain=f"{name}.example.com", system_user=f"site_{name}",
        root_dir=str(root), webroot=str(root / "public"), php_version="8.3",
        ssl_enabled=False, app_type="web", workers=[],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def fs(monkeypatch):
    fake = FakeAcl()
    monkeypatch.setattr(perms, "run", fake.run)
    monkeypatch.setattr(perms, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(perms, "web_user", lambda: WEB)
    monkeypatch.setattr(perms, "mounted_noacl", lambda path: False)
    return fake


@pytest.fixture
def served(fs, monkeypatch):
    """nginx's view: permissions decide between 200 and 403."""
    current = {"site": None}

    def enters(user, test):
        return fs.can_serve(current["site"])

    monkeypatch.setattr(perms, "_as_web_user", enters)
    monkeypatch.setattr(perms, "http_status", lambda domain, https: 200 if fs.can_serve(current["site"]) else 403)
    return current


# --------------------------------------------------------------------------
# The grant (what is written -- the isolation property)
# --------------------------------------------------------------------------


def test_whole_site_gets_read_access_and_a_default_for_new_content(tmp_path, fs):
    site = make_site(tmp_path)
    assert perms.apply(FakeCtx(), site) is True
    root = site.root_dir
    assert ("-m", False, True, f"u:{WEB}:rX", root) in fs.writes       # setfacl -R  -m
    assert ("-m", True, False, f"u:{WEB}:rX", root) in fs.writes       # find ... setfacl -d -m
    assert fs.access[root] == "r-x" and fs.default[root] == "r-x"
    assert fs.access[site.webroot] == "r-x" and fs.default[site.webroot] == "r-x"
    assert fs.access[site.webroot + "/index.html"] == "r-x"


def test_only_the_web_user_is_ever_named_and_modes_are_never_touched(tmp_path, fs):
    perms.apply(FakeCtx(), make_site(tmp_path))
    for _, _, _, spec, _ in fs.writes:
        assert spec.startswith(f"u:{WEB}")
    assert fs.other == []                     # no chmod / chown / usermod


def test_php_scratch_space_is_withheld(tmp_path, fs):
    site = make_site(tmp_path)
    perms.apply(FakeCtx(), site)
    tmp = site.root_dir + "/tmp"
    assert tmp not in fs.access and tmp + "/sessions" not in fs.access
    assert tmp + "/sessions" not in fs.default


def test_other_sites_are_untouched(tmp_path, fs):
    a, b = make_site(tmp_path, "a"), make_site(tmp_path, "b")
    perms.apply(FakeCtx(), a)
    assert not any(path.startswith(b.root_dir) for path in fs.access)


def test_app_sites_need_nothing(tmp_path, fs):
    site = make_site(tmp_path, app_type="node")
    assert perms.apply(FakeCtx(), site) is False
    assert fs.writes == []
    assert perms.check(site).ok


def test_missing_folders_are_skipped_not_fatal(tmp_path, fs):
    site = SimpleNamespace(
        id=1, name="x", domain="x.example", system_user="u", root_dir=str(tmp_path / "no"),
        webroot=str(tmp_path / "no" / "public"), php_version=None, ssl_enabled=False,
        app_type="web", workers=[],
    )
    ctx = FakeCtx()
    assert perms.apply(ctx, site) is False
    assert any("does not exist" in line for line in ctx.lines)


# --------------------------------------------------------------------------
# Reading ACL state
# --------------------------------------------------------------------------


def test_parse_grant_reads_access_default_and_effective_permissions():
    text = (
        "# file: /var/www/shop\n# owner: site_shop\nuser::rwx\nuser:www-data:r-x\n"
        "group::r-x\nmask::r-x\nother::---\ndefault:user::rwx\ndefault:user:www-data:r-x\n"
        "default:group::r-x\ndefault:other::---\n"
    )
    assert perms.parse_grant(text, "www-data") == ("r-x", "r-x")
    assert perms.parse_grant("user::rwx\nother::---\n", "www-data") == (None, None)
    masked = "user:www-data:r-x\t#effective:---\nmask::---\n"
    assert perms.parse_grant(masked, "www-data") == ("---", None)   # chmod 700 locked nginx out


def test_web_user_comes_from_nginx_conf(tmp_path, monkeypatch):
    conf = tmp_path / "nginx.conf"
    conf.write_text("user nginx;\nworker_processes auto;\n")
    monkeypatch.setattr(perms, "NGINX_CONF", conf)
    assert perms.web_user() == "nginx"
    conf.write_text("worker_processes auto;\n")
    assert perms.web_user() == "www-data"
    monkeypatch.setattr(perms, "NGINX_CONF", tmp_path / "missing.conf")
    assert perms.web_user() == "www-data"


def test_mounted_noacl_detects_the_option_on_the_right_mount(tmp_path, monkeypatch):
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "/dev/sda1 / ext4 rw,relatime 0 0\n"
        f"/dev/sdb1 {tmp_path} ext4 rw,noacl 0 0\n"
    )
    monkeypatch.setattr(perms, "PROC_MOUNTS", mounts)
    assert perms.mounted_noacl(tmp_path / "shop") is True
    mounts.write_text("/dev/sda1 / ext4 rw,relatime 0 0\n")
    assert perms.mounted_noacl(tmp_path / "shop") is False
    monkeypatch.setattr(perms, "PROC_MOUNTS", tmp_path / "absent")
    assert perms.mounted_noacl(tmp_path) is False


# --------------------------------------------------------------------------
# Upgrade scenarios: 200 afterwards, whatever the starting state
# --------------------------------------------------------------------------


def _db_site(db, tmp_path, name="shop"):
    site = make_site(tmp_path, name)
    row = Site(
        name=name, domain=site.domain, system_user=site.system_user,
        root_dir=site.root_dir, webroot=site.webroot, php_version="8.3",
    )
    db.add(row)
    db.commit()
    return row


class MigrationCtx(FakeCtx):
    payload = {"all": True}


def _migrate(monkeypatch):
    marked = []
    monkeypatch.setattr(tasks, "mark_permissions_migrated", lambda: marked.append(True))
    ctx = MigrationCtx()
    tasks.repair_site_permissions(ctx)
    return ctx, marked


def test_fresh_site_with_an_index_file_serves_200(tmp_path, fs, served, monkeypatch):
    site = make_site(tmp_path)
    served["site"] = site
    monkeypatch.setattr(sites_service, "sites_root", lambda: tmp_path)
    health_before = perms.check(site)
    assert health_before.http_status == 403          # no grant yet: the original bug
    perms.apply(FakeCtx(), site)
    health = perms.check(site)
    assert health.ok and health.http_status == 200


def test_upgrade_from_the_old_broken_state_serves_200_after_migration(db, tmp_path, fs, served, monkeypatch):
    row = _db_site(db, tmp_path)
    served["site"] = row
    assert perms.check(row).problem == "permissions"       # 403 before

    ctx, marked = _migrate(monkeypatch)

    assert fs.writes, "the grant must be applied"
    health = perms.check(row)
    assert health.ok and health.http_status == 200
    assert marked == [True]
    assert any("Serving normally (HTTP 200)" in line for line in ctx.lines)


def test_upgrade_with_the_manual_acl_already_applied_changes_nothing(db, tmp_path, fs, served, monkeypatch):
    row = _db_site(db, tmp_path)
    served["site"] = row
    fs.grant_by_hand(row.root_dir)                          # setfacl -R -m / -Rd -m by hand
    before = (dict(fs.access), dict(fs.default))
    assert perms.check(row).http_status == 200

    ctx, marked = _migrate(monkeypatch)

    assert fs.writes == []                                  # no setfacl write at all
    assert (fs.access, fs.default) == before                # the hand-made ACL is intact
    assert perms.check(row).http_status == 200
    assert marked == [True]
    assert any("nothing changed" in line for line in ctx.lines)


def test_migration_is_safe_to_run_twice(db, tmp_path, fs, served, monkeypatch):
    row = _db_site(db, tmp_path)
    served["site"] = row
    _migrate(monkeypatch)
    writes_after_first = len(fs.writes)
    _migrate(monkeypatch)
    assert len(fs.writes) == writes_after_first             # second run is a no-op
    assert perms.check(row).http_status == 200


def test_partial_state_is_completed_not_trusted(db, tmp_path, fs, served, monkeypatch):
    """Root has the entries but the files inside do not (a non-recursive fix)."""
    row = _db_site(db, tmp_path)
    served["site"] = row
    fs.access[row.root_dir] = "r-x"
    fs.default[row.root_dir] = "r-x"
    fs.access[row.webroot] = "r-x"
    fs.default[row.webroot] = "r-x"
    # still servable, so the migration leaves it alone...
    _migrate(monkeypatch)
    assert fs.writes == []
    # ...but the Repair button (force) re-states it across the whole tree.
    perms.repair(FakeCtx(), row, force=True)
    assert fs.access[row.webroot + "/index.html"] == "r-x"


def test_a_site_still_blocked_gets_the_full_recursive_grant(db, tmp_path, fs, served, monkeypatch):
    """Looks granted at the top but nginx still cannot read (mask reset by a chmod)."""
    row = _db_site(db, tmp_path)
    served["site"] = row
    fs.access[row.root_dir] = "r-x"
    fs.default[row.root_dir] = "r-x"
    fs.access[row.webroot] = "r--"      # lost x: nginx cannot enter
    fs.default[row.webroot] = "r-x"
    served["site"] = row
    ctx, _ = _migrate(monkeypatch)
    assert fs.access[row.webroot] == "r-x"
    assert perms.check(row).http_status == 200


# --------------------------------------------------------------------------
# Without ACLs: group membership
# --------------------------------------------------------------------------


def test_missing_acl_tools_fall_back_to_group_membership_and_restart_nginx(tmp_path, fs, monkeypatch):
    def no_tools(ctx=None):
        raise perms.PermissionsError("The 'acl' package could not be installed")

    monkeypatch.setattr(perms, "ensure_acl_tools", no_tools)
    site = make_site(tmp_path)
    ctx = FakeCtx()
    assert perms.apply(ctx, site) is True
    assert ["usermod", "-aG", "site_shop", WEB] in fs.other
    assert ["systemctl", "restart", "nginx"] in ctx.commands      # a reload would not do
    assert fs.writes == []
    assert any("group membership" in line for line in ctx.lines)


def test_a_noacl_mount_falls_back_too(tmp_path, fs, monkeypatch):
    monkeypatch.setattr(perms, "mounted_noacl", lambda path: True)
    ctx = FakeCtx()
    perms.apply(ctx, make_site(tmp_path))
    assert ["usermod", "-aG", "site_shop", WEB] in fs.other


def test_a_filesystem_without_acl_support_falls_back(tmp_path, monkeypatch, fs):
    def failing(args, **kw):
        if args[0] == "setfacl":
            return CommandResult(tuple(args), 1, "", "setfacl: x: Operation not supported")
        return fs.run(args, **kw)

    monkeypatch.setattr(perms, "run", failing)
    ctx = FakeCtx()
    perms.apply(ctx, make_site(tmp_path))
    assert ["usermod", "-aG", "site_shop", WEB] in fs.other


def test_group_fallback_failure_is_a_clear_error(tmp_path, monkeypatch, fs):
    monkeypatch.setattr(perms, "mounted_noacl", lambda path: True)

    def failing(args, **kw):
        if args[0] == "usermod":
            return CommandResult(tuple(args), 6, "", "usermod: group 'site_shop' does not exist")
        return fs.run(args, **kw)

    monkeypatch.setattr(perms, "run", failing)
    with pytest.raises(perms.PermissionsError, match="Could not add"):
        perms.apply(FakeCtx(), make_site(tmp_path))


def test_acl_package_is_installed_when_setfacl_is_missing(monkeypatch):
    state = {"installed": False}
    monkeypatch.setattr(perms, "which", lambda name: f"/usr/bin/{name}" if state["installed"] else None)
    installs = []

    def fake_install(ctx, packages, **kw):
        installs.append(list(packages))
        state["installed"] = True

    monkeypatch.setattr("app.services.apt.install", fake_install)
    ctx = FakeCtx()
    perms.ensure_acl_tools(ctx)
    assert installs == [["acl"]]


def test_missing_setfacl_without_a_context_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(perms, "which", lambda name: None)
    with pytest.raises(perms.PermissionsError, match="acl"):
        perms.ensure_acl_tools(None)


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


@pytest.fixture
def health_env(monkeypatch):
    monkeypatch.setattr(perms, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(perms, "web_user", lambda: WEB)
    state = {"can_read": True, "status": 200}
    monkeypatch.setattr(perms, "_as_web_user", lambda user, test: state["can_read"])
    monkeypatch.setattr(perms, "http_status", lambda domain, https: state["status"])
    return state


def test_health_ok(tmp_path, health_env):
    health = perms.check(make_site(tmp_path))
    assert health.ok and health.http_status == 200


def test_health_blames_permissions_when_the_web_user_cannot_enter(tmp_path, health_env):
    health_env["can_read"] = False
    health = perms.check(make_site(tmp_path))
    assert not health.ok and health.problem == "permissions"
    assert "Repair permissions" in health.message


def test_403_with_a_readable_folder_and_no_index_is_not_a_permissions_problem(tmp_path, health_env):
    site = make_site(tmp_path)
    (Path(site.webroot) / "index.html").unlink()
    health_env["status"] = 403
    assert perms.check(site).problem == "no_index"


def test_403_with_an_index_file_still_points_at_repair(tmp_path, health_env):
    health_env["status"] = 403
    assert perms.check(make_site(tmp_path)).problem == "permissions"


def test_500_is_reported(tmp_path, health_env):
    health_env["status"] = 500
    health = perms.check(make_site(tmp_path))
    assert not health.ok and health.problem == "server_error" and health.http_status == 500


def test_unreachable_nginx_is_not_a_failure(tmp_path, health_env):
    health_env["status"] = None
    assert perms.check(make_site(tmp_path)).ok


def test_missing_webroot_is_reported(tmp_path, health_env):
    health = perms.check(make_site(tmp_path, webroot=str(tmp_path / "gone")))
    assert not health.ok and "does not exist" in health.message


def test_check_all_skips_proxied_apps(tmp_path, health_env):
    results = perms.check_all([make_site(tmp_path, "a"), make_site(tmp_path, "b", app_type="node")])
    assert [site.name for site, _ in results] == ["a"]


def test_http_status_probe_sends_the_host_header(monkeypatch):
    sent = []

    class Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def sendall(self, data):
            sent.append(data.decode())

        def recv(self, n):
            return b"HTTP/1.1 403 Forbidden\r\n"

    monkeypatch.setattr(perms.socket, "create_connection", lambda addr, timeout: Sock())
    assert perms.http_status("shop.example.com", https=False) == 403
    assert "Host: shop.example.com" in sent[0]
    monkeypatch.setattr(perms.socket, "create_connection", lambda addr, timeout: (_ for _ in ()).throw(OSError("refused")))
    assert perms.http_status("x", https=False) is None


# --------------------------------------------------------------------------
# Repair
# --------------------------------------------------------------------------


def test_repair_logs_an_error_line_when_still_blocked(tmp_path, fs, monkeypatch):
    monkeypatch.setattr(perms, "_as_web_user", lambda user, test: False)
    ctx = FakeCtx()
    health = perms.repair(ctx, make_site(tmp_path))
    assert not health.ok
    assert any(line.startswith("ERROR") for line in ctx.lines)


# --------------------------------------------------------------------------
# Keeping the ACL when permissions are rewritten
# --------------------------------------------------------------------------


@pytest.fixture
def managed(tmp_path, fs, monkeypatch):
    monkeypatch.setattr(sites_service, "sites_root", lambda: tmp_path)
    site = make_site(tmp_path)
    fs.grant_by_hand(site.root_dir)
    return site


def test_reassert_restores_a_directory_after_chmod(managed, fs):
    target = Path(managed.webroot)
    fs.access[str(target)] = "---"                    # what chmod 700 does to the mask
    perms.reassert(target)
    assert fs.access[str(target)] == "r-x"


def test_reassert_leaves_a_file_the_operator_chmoded_alone(managed, fs):
    secret = Path(managed.webroot) / "index.html"
    fs.access[str(secret)] = "---"
    before = len(fs.writes)
    perms.reassert(secret)
    assert fs.access[str(secret)] == "---" and len(fs.writes) == before


def test_reassert_after_copy_or_extract_covers_the_new_files(managed, fs):
    fresh = Path(managed.webroot) / "uploads"
    (fresh / "deep").mkdir(parents=True)
    (fresh / "deep" / "a.txt").write_text("x")
    perms.reassert(fresh, recursive=True, files=True)
    assert fs.access[str(fresh / "deep" / "a.txt")] == "r-x"
    assert fs.default[str(fresh / "deep")] == "r-x"


def test_reassert_does_nothing_outside_a_granted_site(tmp_path, fs, monkeypatch):
    monkeypatch.setattr(sites_service, "sites_root", lambda: tmp_path)
    site = make_site(tmp_path)                        # no grant at all
    perms.reassert(Path(site.webroot))
    assert fs.writes == []
    perms.reassert(tmp_path.parent)                   # outside any site
    assert fs.writes == []


def test_reassert_never_raises(managed, monkeypatch):
    monkeypatch.setattr(perms, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    perms.reassert(Path(managed.webroot), recursive=True, files=True)


def test_file_manager_chmod_reasserts_directories(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(files_service.permissions_service, "reassert", lambda p, **kw: calls.append((str(p), kw)))
    monkeypatch.setattr(files_service, "resolve", lambda c: Path(c))
    monkeypatch.setattr(files_service, "root", lambda: tmp_path / "elsewhere")
    folder = tmp_path / "public"
    folder.mkdir()
    files_service.chmod(folder, "755")
    files_service.chmod_recursive(folder, "644", "files")
    assert calls[0] == (str(folder), {})
    assert calls[1] == (str(folder), {"recursive": True})


# --------------------------------------------------------------------------
# Site creation: grant before the first reload
# --------------------------------------------------------------------------


@pytest.fixture
def creation(monkeypatch, tmp_path):
    monkeypatch.setattr(sites_service, "sites_root", lambda: tmp_path)
    monkeypatch.setattr(sites_service, "_create_system_user", lambda ctx, u, h: None)
    monkeypatch.setattr(sites_service, "_create_directories", lambda ctx, site: None)
    monkeypatch.setattr(sites_service, "_write_placeholder_page", lambda site: None)
    events = []
    monkeypatch.setattr(sites_service, "render_site", lambda site: events.append("render"))
    monkeypatch.setattr(sites_service, "_reload_services", lambda ctx, site: events.append("reload"))
    monkeypatch.setattr(perms, "apply", lambda ctx, site, **kw: events.append("grant") or True)
    monkeypatch.setattr(
        perms, "check",
        lambda site: events.append("check") or perms.Health(True, message="Serving normally (HTTP 200)"),
    )
    return events


def test_the_grant_happens_before_the_first_nginx_reload_and_the_site_is_then_tested(db, creation):
    ctx = FakeCtx()
    sites_service.create_site(db, ctx, name="shop", domain="shop.example.com", php_version=None)
    assert creation == ["grant", "render", "reload", "check"]
    assert any("Serving normally" in line for line in ctx.lines)


def test_a_failed_grant_warns_but_the_site_is_still_created(db, creation, monkeypatch):
    def boom(ctx, site, **kw):
        raise perms.PermissionsError("the filesystem is not usable")

    monkeypatch.setattr(perms, "apply", boom)
    ctx = FakeCtx()
    site = sites_service.create_site(db, ctx, name="shop", domain="shop.example.com")
    assert site.id
    assert any("WARNING" in line for line in ctx.lines)
    assert any("Repair permissions" in line for line in ctx.lines)


def test_a_403_after_creation_is_an_error_line(db, creation, monkeypatch):
    monkeypatch.setattr(
        perms, "check",
        lambda site: perms.Health(False, problem="permissions", message="nginx cannot read it"),
    )
    ctx = FakeCtx()
    sites_service.create_site(db, ctx, name="shop", domain="shop.example.com")
    assert any(line.startswith("ERROR") for line in ctx.lines)


def test_app_sites_skip_the_grant_and_the_check(db, creation, monkeypatch):
    from app.services import apps as apps_service

    monkeypatch.setattr(apps_service, "require_runtime", lambda t: None)
    monkeypatch.setattr(apps_service, "port_in_use_locally", lambda p: False)
    sites_service.create_site(db, FakeCtx(), name="api", domain="api.example.com", app_type="node")
    assert "grant" not in creation and "check" not in creation


# --------------------------------------------------------------------------
# Jobs, startup migration, rebuild
# --------------------------------------------------------------------------


def test_migration_is_not_marked_done_when_a_site_fails(db, tmp_path, monkeypatch):
    _db_site(db, tmp_path)
    monkeypatch.setattr(
        perms, "repair",
        lambda ctx, site, **kw: perms.Health(False, problem="permissions", message="blocked"),
    )
    marked = []
    monkeypatch.setattr(tasks, "mark_permissions_migrated", lambda: marked.append(True))
    with pytest.raises(jobs.JobFailed, match="shop.example.com"):
        tasks.repair_site_permissions(MigrationCtx())
    assert marked == []


def test_migration_does_not_force_but_the_repair_buttons_do(db, tmp_path, monkeypatch):
    row = _db_site(db, tmp_path)
    forced = []
    monkeypatch.setattr(perms, "repair", lambda ctx, site, force=True: forced.append(force) or perms.Health(True))
    monkeypatch.setattr(tasks, "mark_permissions_migrated", lambda: None)

    tasks.repair_site_permissions(MigrationCtx())                       # upgrade migration

    class AllForced(FakeCtx):
        payload = {"all": True, "force": True}

    tasks.repair_site_permissions(AllForced())                          # "repair all" button

    class One(FakeCtx):
        payload = {"site_id": row.id}

    tasks.repair_site_permissions(One())                                # per-site button
    assert forced == [False, True, True]


def test_apps_are_not_part_of_the_migration(db, tmp_path, monkeypatch):
    _db_site(db, tmp_path, "a")
    db.add(Site(name="api", domain="api.example", system_user="site_api", root_dir="/var/www/api",
                webroot="/var/www/api", app_type="node", app_port=3001))
    db.commit()
    seen = []
    monkeypatch.setattr(perms, "repair", lambda ctx, site, **kw: seen.append(site.name) or perms.Health(True))
    monkeypatch.setattr(tasks, "mark_permissions_migrated", lambda: None)
    tasks.repair_site_permissions(MigrationCtx())
    assert seen == ["a"]


def test_marker_file_is_written_under_the_state_dir(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config.get_settings(), "state_dir", tmp_path)
    tasks.mark_permissions_migrated()
    assert (tmp_path / tasks.PERMISSIONS_MIGRATION_MARKER).exists()


def test_startup_migration_queues_one_job_when_sites_exist_and_no_marker(db, tmp_path, monkeypatch):
    import app.main as main

    _db_site(db, tmp_path)
    monkeypatch.setattr(main.settings, "dev_mode", False)
    monkeypatch.setattr(main.settings, "state_dir", tmp_path / "state")
    monkeypatch.setattr(jobs.worker, "submit", lambda job_id: None)
    main._migrate_site_permissions()
    queued = db.scalars(select(Job).where(Job.kind == "site.repair_permissions")).all()
    assert len(queued) == 1 and json.loads(queued[0].payload) == {"all": True}


def test_startup_migration_does_nothing_once_marked_or_with_no_sites(db, tmp_path, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main.settings, "dev_mode", False)
    monkeypatch.setattr(main.settings, "state_dir", tmp_path / "state")
    monkeypatch.setattr(jobs.worker, "submit", lambda job_id: None)

    main._migrate_site_permissions()                      # no sites: marks itself done
    assert (tmp_path / "state" / tasks.PERMISSIONS_MIGRATION_MARKER).exists()
    assert db.scalars(select(Job)).all() == []

    _db_site(db, tmp_path)
    main._migrate_site_permissions()                      # marker present: skipped
    assert db.scalars(select(Job)).all() == []


def test_rebuild_reapplies_the_grant_without_forcing(db, tmp_path, monkeypatch):
    row = _db_site(db, tmp_path)
    applied = []
    monkeypatch.setattr(perms, "apply", lambda ctx, s, **kw: applied.append((s.name, kw)))
    monkeypatch.setattr(sites_service, "render_site", lambda s: None)
    monkeypatch.setattr("app.providers.all_providers", lambda: [])
    monkeypatch.setattr("app.services.fail2ban.is_installed", lambda: False)
    renderer.rebuild_all(FakeCtx())
    assert applied == [(row.name, {})]


# --------------------------------------------------------------------------
# Web
# --------------------------------------------------------------------------


@pytest.fixture
def web(monkeypatch, db, admin):
    monkeypatch.setattr(jobs.worker, "submit", lambda job_id: None)
    fake = MagicMock()
    fake.is_installed.return_value = True
    fake.installed_versions.return_value = ["8.3"]
    monkeypatch.setattr("app.routers.sites.get_provider", lambda key: fake)
    with TestClient(create_app(), follow_redirects=False) as client:
        assert client.post("/login", data={"username": "admin", "password": "correct-horse-battery"}).status_code == 303
        yield client


def _csrf(client, path):
    page = client.get(path)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


def test_detail_page_has_the_access_panel_and_repair_button_for_php_sites(web, db, tmp_path):
    site = _db_site(db, tmp_path)
    page = web.get(f"/sites/{site.id}")
    assert "Web server access" in page.text and "Repair permissions" in page.text
    assert "/static/site-health.js" in page.text
    assert "No PHP version is assigned" not in page.text      # this site has PHP 8.3


def test_no_php_notice_on_a_static_site(web, db, tmp_path):
    site = _db_site(db, tmp_path)
    site.php_version = None
    db.commit()
    assert "No PHP version is assigned" in web.get(f"/sites/{site.id}").text


def test_app_sites_do_not_get_the_access_panel(web, db):
    db.add(Site(name="api", domain="api.example", system_user="site_api", root_dir="/var/www/api",
                webroot="/var/www/api", app_type="node", app_port=3001))
    db.commit()
    site = db.scalars(select(Site)).first()
    assert "Web server access" not in web.get(f"/sites/{site.id}").text


def test_site_health_endpoint(web, db, tmp_path, monkeypatch):
    site = _db_site(db, tmp_path)
    monkeypatch.setattr(
        perms, "check",
        lambda s: perms.Health(False, problem="permissions", message="nginx cannot read it", http_status=403),
    )
    assert web.get(f"/sites/{site.id}/health").json() == {
        "ok": False, "problem": "permissions", "message": "nginx cannot read it", "http_status": 403,
    }
    assert web.get("/sites/9999/health").status_code == 404


def test_sites_health_lists_failures_but_not_empty_folders(web, db, tmp_path, monkeypatch):
    bad = _db_site(db, tmp_path, "bad")
    empty = _db_site(db, tmp_path, "empty")
    broken = _db_site(db, tmp_path, "broken")
    answers = {
        bad.id: perms.Health(False, "permissions", "nginx cannot read it", 403),
        empty.id: perms.Health(False, "no_index", "no index", 403),
        broken.id: perms.Health(False, "server_error", "HTTP 500", 500),
    }
    monkeypatch.setattr(perms, "check", lambda site: answers[site.id])
    data = web.get("/sites/health").json()
    assert sorted(p["domain"] for p in data["problems"]) == ["bad.example.com", "broken.example.com"]


def test_sites_page_has_the_banner_and_script(web, db, tmp_path):
    _db_site(db, tmp_path)
    page = web.get("/sites")
    assert 'id="sites-health-banner"' in page.text and "/static/sites-health.js" in page.text
    assert "/sites/permissions/repair-all" in page.text


def test_repair_buttons_enqueue_jobs(web, db, tmp_path):
    site = _db_site(db, tmp_path)
    token = _csrf(web, f"/sites/{site.id}")
    assert web.post(f"/sites/{site.id}/permissions/repair", data={"csrf_token": token}).status_code == 303
    job = db.scalars(select(Job).order_by(Job.id.desc())).first()
    assert job.kind == "site.repair_permissions" and json.loads(job.payload) == {"site_id": site.id}

    assert web.post("/sites/permissions/repair-all", data={"csrf_token": token}).status_code == 303
    job = db.scalars(select(Job).order_by(Job.id.desc())).first()
    assert json.loads(job.payload) == {"all": True, "force": True}


def test_repair_is_refused_for_app_sites(web, db):
    db.add(Site(name="api", domain="api.example", system_user="site_api", root_dir="/var/www/api",
                webroot="/var/www/api", app_type="node", app_port=3001))
    db.commit()
    site = db.scalars(select(Site)).first()
    response = web.post(
        f"/sites/{site.id}/permissions/repair",
        data={"csrf_token": _csrf(web, "/sites")},
    )
    assert "error=" in response.headers["location"]
    assert db.scalars(select(Job)).all() == []


# --------------------------------------------------------------------------
# vhost without PHP
# --------------------------------------------------------------------------


def _vhost(**extra):
    site = SimpleNamespace(
        name="shop", domain="shop.example.com", webroot="/var/www/shop", ssl_enabled=False,
        redirect_www=True, upload_limit_mb=64, aliases=[], app_type="web",
    )
    context = {
        "site": site, "server_names": [site.domain], "fpm_socket": None,
        "acme_root": "/var/www/lite-panel-acme", "www_redirect_only": True, "www_https_redirect": False,
    }
    context.update(extra)
    return renderer.render("vhost.conf.j2", context)


def test_vhost_without_php_serves_static_files_and_does_not_fall_back_to_index_php():
    out = _vhost()
    assert "index index.html index.htm;" in out
    assert "try_files $uri $uri/ =404;" in out
    assert "return 404;" in out                            # .php files stay unexecuted
    assert "/index.php?$query_string" not in out


def test_vhost_with_php_is_unchanged():
    out = _vhost(fpm_socket="/run/php/x.sock")
    assert "index index.php index.html index.htm;" in out
    assert "try_files $uri $uri/ /index.php?$query_string;" in out
    assert "fastcgi_pass unix:/run/php/x.sock;" in out
