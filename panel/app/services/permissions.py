"""Letting nginx read a site's files without opening them to other sites.

A site's home is ``750 site_<name>:site_<name>``: other sites' PHP-FPM users
must not be able to walk in and read each other's ``.env`` files.  But nginx
(``www-data``) also reads the webroot directly -- ``index.html``, assets,
``try_files`` and the ``index.php`` existence check all happen in nginx, before
FPM is involved -- so without access it answers 403 for every request.

The grant is a POSIX ACL for exactly one extra user, the web server::

    setfacl -R -m u:www-data:rX  /var/www/<site>      # what is there now
    setfacl -d -m u:www-data:rX  <every directory>    # what is created later

so nothing is made world-readable and one site's user still cannot read
another's directory.  ``apply`` is safe to re-run and never removes anything an
operator set by hand: a site that already carries the grant (for example one
fixed manually with those two commands) is left completely untouched.

Where ACLs are unavailable (no ``acl`` package that can be installed, or a
filesystem mounted ``noacl``) the fallback is membership of the site's group,
which the 750 mode already lets read; nginx is then restarted, because a reload
does not refresh the groups of running workers.
"""

from __future__ import annotations

import logging
import re
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from app.services import apps as apps_service
from app.shell import CommandError, run, which

logger = logging.getLogger(__name__)

NGINX_CONF = Path("/etc/nginx/nginx.conf")
PROC_MOUNTS = Path("/proc/mounts")
_NGINX_USER = re.compile(r"^\s*user\s+([A-Za-z0-9_.-]+)", re.MULTILINE)
_EFFECTIVE = re.compile(r"#effective:([r-][w-][x-])")


class PermissionsError(RuntimeError):
    """The web server could not be granted access; the message says why."""


# --------------------------------------------------------------------------
# Who and what
# --------------------------------------------------------------------------


def needs_webserver_access(site) -> bool:
    """Only sites nginx serves files for.  A Node/Python app is proxied."""
    return not apps_service.is_app(site)


def web_user() -> str:
    """The account nginx workers run as (the ``user`` line of nginx.conf)."""
    try:
        match = _NGINX_USER.search(NGINX_CONF.read_text(encoding="utf-8"))
    except OSError:
        match = None
    return match.group(1) if match else "www-data"


def _spec(user: str) -> str:
    return f"u:{user}:rX"


# --------------------------------------------------------------------------
# Reading the current state
# --------------------------------------------------------------------------


def parse_grant(getfacl_output: str, user: str) -> Tuple[Optional[str], Optional[str]]:
    """(access, default) permissions of ``user`` from ``getfacl`` output.

    ``None`` when there is no such entry.  The *effective* permissions (after
    the mask) are what count, so a ``#effective:r--`` note wins over the entry.
    """
    access = default = None
    for raw in getfacl_output.splitlines():
        line = raw.strip()
        is_default = line.startswith("default:")
        body = line[len("default:"):] if is_default else line
        fields = body.split("\t")[0].split(":")
        if len(fields) < 3 or fields[0] != "user" or fields[1] != user:
            continue
        perms = fields[2].strip()[:3]
        effective = _EFFECTIVE.search(raw)
        if effective:
            perms = "".join(a if b != "-" else "-" for a, b in zip(perms, effective.group(1)))
        if is_default:
            default = perms
        else:
            access = perms
    return access, default


def _readable(perms: Optional[str]) -> bool:
    return bool(perms) and perms[0] == "r" and perms[2] == "x"


def read_grant(path: Path, user: str) -> Tuple[Optional[str], Optional[str]]:
    result = run(["getfacl", "-p", "-c", str(path)], check=False, timeout=30)
    if not result.ok:
        return None, None
    return parse_grant(result.stdout, user)


def already_granted(root: Path, webroot: Path, user: str) -> bool:
    """True when the site carries the full grant (so nothing needs touching)."""
    for path in {root, webroot}:
        access, default = read_grant(path, user)
        if not (_readable(access) and _readable(default)):
            return False
    return True


# --------------------------------------------------------------------------
# Tooling
# --------------------------------------------------------------------------


def ensure_acl_tools(ctx=None) -> None:
    """Make sure ``setfacl`` exists, installing the ``acl`` package if needed."""
    if which("setfacl") and which("getfacl"):
        return
    if ctx is None:
        raise PermissionsError("The 'acl' package is not installed (setfacl is missing).")
    from app.services import apt

    ctx.log("Installing the 'acl' package (needed to let nginx read site files)")
    try:
        apt.install(ctx, ["acl"])
    except (CommandError, RuntimeError) as exc:
        raise PermissionsError(f"Could not install the 'acl' package: {exc}") from exc
    if not (which("setfacl") and which("getfacl")):
        raise PermissionsError("The 'acl' package was installed but setfacl is still missing.")


def mounted_noacl(path: Path) -> bool:
    """Whether the filesystem holding ``path`` is mounted with ``noacl``."""
    try:
        target = str(path.resolve())
        best, options = "", ""
        for line in PROC_MOUNTS.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) < 4:
                continue
            mount, opts = fields[1], fields[3]
            if (target == mount or target.startswith(mount.rstrip("/") + "/")) and len(mount) > len(best):
                best, options = mount, opts
        return "noacl" in options.split(",")
    except OSError:
        return False


def _setfacl(args: List[str], *, what: str) -> None:
    result = run(["setfacl", *args], check=False, timeout=900)
    if result.ok:
        return
    message = (result.stderr or result.stdout or "").strip()
    if "not supported" in message.lower():
        raise PermissionsError(
            "This filesystem does not support ACLs (mounted with noacl, or an unsupported type)."
        )
    raise PermissionsError(f"Could not set permissions ({what}): {message[:300]}")


def _grant_tree(path: Path, user: str) -> None:
    """r-X on a tree now, and as a default so new content inherits it."""
    spec = _spec(user)
    _setfacl(["-R", "-m", spec, str(path)], what=str(path))
    # Default entries only exist on directories; ``find`` hands setfacl just those.
    result = run(
        ["find", str(path), "-type", "d", "-exec", "setfacl", "-d", "-m", spec, "{}", "+"],
        check=False,
        timeout=900,
    )
    if not result.ok:
        raise PermissionsError(
            f"Could not set default permissions on {path}: {(result.stderr or '').strip()[:300]}"
        )


def _revoke_tree(path: Path, user: str) -> None:
    """Take the grant away again (PHP's session / upload scratch space)."""
    if not path.is_dir():
        return
    entry = f"u:{user}"
    run(["setfacl", "-R", "-x", entry, str(path)], check=False)
    run(
        ["find", str(path), "-type", "d", "-exec", "setfacl", "-d", "-x", entry, "{}", "+"],
        check=False,
    )


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------


def _fallback_group(ctx, site, user: str) -> None:
    """No ACLs: put the web user in the site's group (750 already lets that
    group read) and restart nginx so running workers pick the group up."""
    group = site.system_user
    result = run(["usermod", "-aG", group, user], check=False, timeout=60)
    if not result.ok:
        raise PermissionsError(
            f"Could not add {user} to group {group}: {(result.stderr or '').strip()[:200]}"
        )
    ctx.log(f"Added {user} to group {group}; restarting nginx so the new group takes effect")
    ctx.run(["systemctl", "restart", "nginx"])


def apply(ctx, site, *, force: bool = False) -> bool:
    """Grant nginx access to a site.  Returns False when nothing applied.

    With ``force`` the ACL is re-stated across the whole tree even when the
    site already looks fixed (the Repair button).  Raises
    :class:`PermissionsError` with a readable message on failure.
    """
    if not needs_webserver_access(site):
        return False
    root = Path(site.root_dir)
    webroot = Path(site.webroot)
    if not root.is_dir() or not webroot.is_dir():
        ctx.log(f"Skipping web server access for {site.domain}: {webroot} does not exist yet")
        return False

    user = web_user()
    try:
        ensure_acl_tools(ctx)
        if mounted_noacl(root):
            raise PermissionsError("The filesystem is mounted with noacl.")

        if not force and already_granted(root, webroot, user):
            ctx.log(f"{site.domain}: {user} already has access (ACL in place, nothing changed)")
            return True

        _grant_tree(root, user)
        # PHP's session / upload scratch space is never for the web server,
        # even though the grant above covers the whole site folder.
        _revoke_tree(root / "tmp", user)
    except PermissionsError as exc:
        ctx.log(f"ACLs are not available ({exc}); using group membership instead")
        _fallback_group(ctx, site, user)
        return True

    ctx.log(f"{user} can now read {root} (ACL; other sites still cannot)")
    return True


def reassert(path, *, recursive: bool = False, files: bool = False) -> None:
    """Restore nginx's access after an operation rewrote permissions.

    ``chmod`` on an ACL'd directory resets the mask to the group bits, so
    ``chmod 700 public`` silently locks nginx out (403).  Called after file
    manager chmod / copy / extract.  Directories are re-granted; a *file* the
    operator chmod'ed is left exactly as they set it unless ``files`` is set,
    which is for content that was just created (copy, extract).

    Only acts inside a site folder that already carries the grant, and never
    raises: it must not turn a successful file operation into an error.
    """
    try:
        target = Path(path)
        if not (which("setfacl") and which("getfacl")):
            return
        user = web_user()
        site_root = _site_root_of(target)
        if site_root is None:
            return
        access, default = read_grant(site_root, user)
        if not (_readable(access) and _readable(default)):
            return
        spec = _spec(user)
        if files and (recursive or target.is_dir()):
            _grant_tree(target, user)
        elif recursive and target.is_dir():
            for flags in ([], ["-d"]):
                run(
                    ["find", str(target), "-type", "d", "-exec", "setfacl", *flags, "-m", spec, "{}", "+"],
                    check=False, timeout=900,
                )
        elif target.is_dir():
            _setfacl(["-m", spec, str(target)], what=str(target))
            _setfacl(["-d", "-m", spec, str(target)], what=str(target))
        elif files:
            _setfacl(["-m", spec, str(target)], what=str(target))
    except (PermissionsError, OSError, CommandError) as exc:
        logger.debug("could not re-assert web server access on %s: %s", path, exc)


def _site_root_of(path: Path) -> Optional[Path]:
    """``/var/www/<site>`` for a path somewhere inside one."""
    from app.services.sites import sites_root

    base = sites_root()
    try:
        relative = path.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return None
    if not relative.parts:
        return None
    return base / relative.parts[0]


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


@dataclass
class Health:
    ok: bool
    problem: str = ""          # "" | "permissions" | "no_index" | "server_error" | "unreachable"
    message: str = "Serving normally"
    http_status: Optional[int] = None


def _as_web_user(user: str, test: List[str]) -> bool:
    result = run(["runuser", "-u", user, "--", *test], check=False, timeout=15)
    return result.ok


def http_status(domain: str, *, https: bool) -> Optional[int]:
    """GET / on this machine with the site's Host header (and SNI for HTTPS),
    the equivalent of ``curl -sk --resolve <domain>:443:127.0.0.1 https://<domain>/``."""
    try:
        if https:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            raw = socket.create_connection(("127.0.0.1", 443), timeout=5)
            sock = context.wrap_socket(raw, server_hostname=domain)
        else:
            sock = socket.create_connection(("127.0.0.1", 80), timeout=5)
        with sock:
            sock.sendall(
                f"GET / HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n".encode("ascii")
            )
            first = sock.recv(64).decode("latin-1", "replace")
        match = re.match(r"HTTP/\d\.\d (\d{3})", first)
        return int(match.group(1)) if match else None
    except (OSError, ValueError):
        return None


def check(site) -> Health:
    """Can nginx actually serve this site?

    First the cause, checked as the web user itself (so it is exact); then the
    symptom, a real request.  A 403 with working permissions means there is
    simply no index file, which is a different fix and says so.
    """
    if not needs_webserver_access(site):
        return Health(ok=True, message="Proxied to the application; nginx reads no files.")

    webroot = Path(site.webroot)
    if not webroot.is_dir():
        return Health(ok=False, problem="unreachable", message=f"{webroot} does not exist.")

    user = web_user()
    if which("runuser"):
        can_enter = _as_web_user(user, ["test", "-x", str(webroot)]) and _as_web_user(
            user, ["test", "-r", str(webroot)]
        )
        if not can_enter:
            return Health(
                ok=False,
                problem="permissions",
                message=(
                    f"nginx ({user}) cannot read {webroot}, so every request returns 403. "
                    "Use Repair permissions."
                ),
                http_status=403,
            )

    status = http_status(site.domain, https=bool(site.ssl_enabled))
    if status == 403:
        has_index = any((webroot / name).exists() for name in ("index.html", "index.htm", "index.php"))
        if not has_index:
            return Health(
                ok=False,
                problem="no_index",
                message="The folder has no index.html or index.php, so nginx refuses to list it.",
                http_status=403,
            )
        return Health(
            ok=False,
            problem="permissions",
            message="nginx answers 403 although the folder is readable. Use Repair permissions.",
            http_status=403,
        )
    if status == 500:
        return Health(
            ok=False,
            problem="server_error",
            message="The site answers HTTP 500. Check its error log on the Logs page.",
            http_status=500,
        )
    if status is None:
        return Health(ok=True, message="Permissions are in place (nginx did not answer a test request).")
    return Health(ok=True, message=f"Serving normally (HTTP {status})", http_status=status)


# --------------------------------------------------------------------------
# Repair (button, rebuild and the upgrade migration)
# --------------------------------------------------------------------------


def repair(ctx, site, *, force: bool = True) -> Health:
    """Grant access and verify.

    ``force=False`` (the migration) leaves an already-fixed site alone and only
    re-states the ACL across the tree when the site is still not served.
    """
    apply(ctx, site, force=force)
    health = check(site)
    if not health.ok and health.problem == "permissions" and not force:
        ctx.log(f"{site.domain}: still blocked, re-applying the ACL across the whole folder")
        apply(ctx, site, force=True)
        health = check(site)
    if health.ok:
        ctx.log(f"{site.domain}: {health.message}")
    else:
        ctx.log(f"ERROR {site.domain}: {health.message}")
    return health


def check_all(sites) -> List[Tuple[object, Health]]:
    """Health of every site nginx serves files for."""
    return [(site, check(site)) for site in sites if needs_webserver_access(site)]
