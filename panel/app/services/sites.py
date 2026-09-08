"""Site lifecycle.

A site is one object owning its system user, webroot, PHP-FPM pool, nginx
vhost, SSL state and FTP login.  Creating one is a single transaction across
all of those, and deleting one unwinds them together -- which is the whole
reason this exists as a service rather than as five unrelated screens.

Every path and identifier here has been through :mod:`app.validators` before
it reaches a command, and every command is an argument list.
"""

from __future__ import annotations

import logging
import pwd
from pathlib import Path
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.config import get_settings
from app.models import Site
from app.providers import get_provider
from app.services import cron as cron_service
from app.services import renderer, tuning
from app.shell import run
from app.validators import (
    ValidationError,
    site_username,
    validate_domain,
    validate_php_version,
    validate_site_name,
)

logger = logging.getLogger(__name__)

FPM_SOCKET_DIR = Path("/run/php")
PHP_LOG_DIR = Path("/var/log/php")

# The shell site accounts get. They exist to own files and run FPM workers;
# none of them should be able to open an interactive login.
SITE_SHELL = "/usr/sbin/nologin"
# The shell an FTP-enabled account needs: vsftpd refuses to authenticate a
# user whose shell is not listed in /etc/shells, and nologin is not.
FTP_SHELL = "/bin/false"


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def sites_root() -> Path:
    return get_settings().sites_root


def root_dir_for(name: str) -> Path:
    """Where a site's files live: /var/www/<name>.

    Deliberately the plain site name, not the "site_"-prefixed system
    username -- the username needs that prefix to dodge reserved account
    names (see validators.site_username), but the directory a person browses
    to over FTP or in the file manager should just be /var/www/blog, not
    /var/www/site_blog.
    """
    return sites_root() / validate_site_name(name)


def webroot_for(name: str, subfolder: str = "") -> Path:
    base = root_dir_for(name)
    return base / subfolder if subfolder else base


def socket_for(name: str) -> Path:
    return FPM_SOCKET_DIR / f"lite-panel-{validate_site_name(name)}.sock"


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------


def create_site(
    db: OrmSession,
    ctx,
    *,
    name: str,
    domain: str,
    php_version: Optional[str] = None,
    subfolder: str = "",
    redirect_www: bool = True,
) -> Site:
    """Create a site and everything it owns."""
    name = validate_site_name(name)
    domain = validate_domain(domain)
    username = site_username(name)
    if php_version:
        php_version = validate_php_version(php_version)

    if db.scalar(select(Site).where(Site.name == name)):
        raise ValidationError(f"A site named '{name}' already exists.")
    if db.scalar(select(Site).where(Site.domain == domain)):
        raise ValidationError(f"{domain} is already served by another site.")
    if php_version and not get_provider("php").is_version_installed(php_version):
        raise ValidationError(f"PHP {php_version} is not installed.")

    # Normally already there (nginx's own package creates it), but don't
    # depend on install order -- useradd --home-dir only creates the final
    # directory, not missing parents.
    sites_root().mkdir(parents=True, exist_ok=True)

    root_dir = root_dir_for(name)
    webroot = webroot_for(name, subfolder)

    site = Site(
        name=name,
        domain=domain,
        system_user=username,
        root_dir=str(root_dir),
        webroot=str(webroot),
        php_version=php_version,
        redirect_www=redirect_www,
    )

    ctx.log(f"Creating site {domain}")
    _create_system_user(ctx, username, root_dir)
    _create_directories(ctx, site)
    _write_placeholder_page(site)

    db.add(site)
    db.flush()  # assign the id before config is rendered against it

    render_site(site)
    ctx.log(f"rendered configuration for {domain}")

    _reload_services(ctx, site)

    db.commit()
    ctx.log(f"Site {domain} is live at http://{domain}")
    return site


def _create_system_user(ctx, username: str, home: Path) -> None:
    """Create the site's dedicated account, if it isn't already there."""
    if _user_exists(username):
        ctx.log(f"system user {username} already exists")
        return

    ctx.check(
        [
            "useradd",
            "--create-home",
            "--home-dir", str(home),
            "--shell", SITE_SHELL,
            # A per-site group of the same name; nginx is added to nothing, it
            # reaches the site only through the FPM socket.
            "--user-group",
            username,
        ]
    )
    ctx.log(f"created system user {username}")


def _user_exists(username: str) -> bool:
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def _create_directories(ctx, site: Site) -> None:
    root = Path(site.root_dir)
    webroot = Path(site.webroot)
    sessions = root / "tmp" / "sessions"
    tmp = root / "tmp" / "upload"

    for directory in (root, webroot, sessions, tmp):
        directory.mkdir(parents=True, exist_ok=True)

    PHP_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # The site user owns its whole tree.
    run(["chown", "-R", f"{site.system_user}:{site.system_user}", str(root)])
    # 750, not 755: another site's user must not be able to walk in here.
    # nginx never reads these files directly -- it goes through the FPM
    # socket -- so it does not need access.
    run(["chmod", "750", str(root)])
    run(["chmod", "700", str(sessions)])
    ctx.log(f"prepared {root}")


def _write_placeholder_page(site: Site) -> None:
    index = Path(site.webroot) / "index.html"
    if index.exists():
        return
    renderer.render_to_file("placeholder.html.j2", index, {"site": site})
    run(["chown", f"{site.system_user}:{site.system_user}", str(index)])


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_site(site: Site) -> None:
    """Write the vhost and FPM pool for one site.

    Safe to call repeatedly: both writes are idempotent, which is what lets
    ``lite-panel rebuild`` reconstruct every site from the database.
    """
    from app.providers.certbot import ACME_ROOT

    nginx = get_provider("nginx")
    php = get_provider("php")

    server_names = [site.domain] + [alias.domain for alias in site.aliases]
    if site.redirect_www:
        # www is handled by its own redirect server block.
        pass
    else:
        server_names.append(f"www.{site.domain}")

    fpm_socket = str(socket_for(site.name)) if site.php_version else None

    renderer.render_to_file(
        "vhost.conf.j2",
        nginx.config_path(site.name),
        {
            "site": site,
            "server_names": server_names,
            "fpm_socket": fpm_socket,
            "acme_root": str(ACME_ROOT),
        },
    )
    nginx.enable_site(site.name)

    if site.php_version:
        renderer.render_to_file(
            "fpm-pool.conf.j2",
            _pool_path(site),
            {
                "site": site,
                "socket_path": fpm_socket,
                "session_path": str(Path(site.root_dir) / "tmp" / "sessions"),
                "tmp_path": str(Path(site.root_dir) / "tmp" / "upload"),
                "fpm": tuning.fpm_pool_settings(),
                "php": tuning.php_settings(),
            },
        )
    else:
        _remove_all_pools(site)

    _ = php  # provider held for symmetry; socket path is derived above


def _pool_path(site: Site, version: Optional[str] = None) -> Path:
    version = version or site.php_version
    return Path(f"/etc/php/{version}/fpm/pool.d/lite-panel-{site.name}.conf")


def _remove_all_pools(site: Site) -> None:
    """Drop this site's pool from every installed PHP version.

    Called when a site's version changes or PHP is removed from it, so a stale
    pool cannot keep a socket alive on the old version.
    """
    for version in get_provider("php").installed_versions():
        renderer.remove_file(_pool_path(site, version))


# --------------------------------------------------------------------------
# Changes
# --------------------------------------------------------------------------


def set_php_version(db: OrmSession, ctx, site: Site, version: Optional[str]) -> None:
    """Move a site to a different PHP version.

    The operation per-site PHP versions exist for, and the one a single shared
    FPM pool cannot offer at all.
    """
    previous = site.php_version
    if version:
        version = validate_php_version(version)
        if not get_provider("php").is_version_installed(version):
            raise ValidationError(f"PHP {version} is not installed.")

    if previous == version:
        ctx.log(f"{site.domain} is already on PHP {version or 'none'}")
        return

    ctx.log(f"Moving {site.domain} from PHP {previous or 'none'} to {version or 'none'}")

    # Remove the old pool first; leaving it behind would keep a second FPM
    # process listening on the same socket path.
    _remove_all_pools(site)

    site.php_version = version
    db.flush()
    render_site(site)

    php = get_provider("php")
    for item in {previous, version} - {None}:
        ctx.run(["systemctl", "restart", php.service_for(item)])

    get_provider("nginx").reload(ctx)
    db.commit()
    ctx.log(f"{site.domain} now runs PHP {version or 'no PHP'}")


def enable_ssl(db: OrmSession, ctx, site: Site, *, email: Optional[str] = None,
               staging: bool = False) -> None:
    """Obtain a certificate, then re-render the vhost with TLS enabled."""
    certbot = get_provider("certbot")

    # Issue against the plain HTTP vhost that is already serving, so the
    # ACME challenge can be answered before any redirect exists.
    certbot.issue(ctx, site.domain, email=email, include_www=site.redirect_www, staging=staging)

    site.ssl_enabled = True
    db.flush()
    render_site(site)
    get_provider("nginx").reload(ctx)
    db.commit()
    ctx.log(f"HTTPS is live for {site.domain}")


def disable_ssl(db: OrmSession, ctx, site: Site) -> None:
    site.ssl_enabled = False
    db.flush()
    render_site(site)
    get_provider("nginx").reload(ctx)
    db.commit()
    ctx.log(f"HTTPS disabled for {site.domain}; the certificate was kept")


# --------------------------------------------------------------------------
# Deletion
# --------------------------------------------------------------------------


def delete_site(db: OrmSession, ctx, site: Site, *, remove_files: bool = False) -> None:
    """Remove a site.

    Files are kept unless explicitly asked for: deleting a site by mistake
    should be recoverable, and the account's home directory is the only copy
    of the operator's content on the box.
    """
    domain, name, username = site.domain, site.name, site.system_user
    root_dir = site.root_dir
    version = site.php_version

    ctx.log(f"Deleting site {domain}")

    get_provider("nginx").remove_site(name)
    _remove_all_pools(site)

    try:
        get_provider("vsftpd").remove_user_config(username)
    except Exception as exc:  # noqa: BLE001 - vsftpd may not be installed
        ctx.log(f"note: {exc}")
    _remove_from_ftp_userlist(username)

    try:
        cron_service.remove_crontab(username)
    except Exception as exc:  # noqa: BLE001 - cron may not be installed
        ctx.log(f"note: {exc}")

    db.delete(site)
    db.commit()

    if version:
        ctx.run(["systemctl", "restart", get_provider("php").service_for(version)])
    get_provider("nginx").reload(ctx)

    if remove_files:
        ctx.run(["userdel", "--remove", username])
        ctx.log(f"removed system user {username} and {root_dir}")
    else:
        ctx.run(["userdel", username])
        ctx.log(f"removed system user {username}; files kept at {root_dir}")

    ctx.log(f"Site {domain} deleted")


# --------------------------------------------------------------------------
# FTP account list
# --------------------------------------------------------------------------

FTP_USERLIST = Path("/etc/vsftpd.userlist")


def _read_ftp_userlist() -> List[str]:
    if not FTP_USERLIST.exists():
        return []
    return [
        line.strip()
        for line in FTP_USERLIST.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def add_to_ftp_userlist(username: str) -> None:
    """vsftpd only lets in accounts named here (userlist_deny=NO)."""
    users = _read_ftp_userlist()
    if username not in users:
        users.append(username)
        renderer.write_atomic(FTP_USERLIST, "\n".join(sorted(users)) + "\n")


def _remove_from_ftp_userlist(username: str) -> None:
    users = [u for u in _read_ftp_userlist() if u != username]
    if FTP_USERLIST.exists():
        renderer.write_atomic(FTP_USERLIST, "\n".join(sorted(users)) + ("\n" if users else ""))


def _reload_services(ctx, site: Site) -> None:
    if site.php_version:
        ctx.run(["systemctl", "restart", get_provider("php").service_for(site.php_version)])
    get_provider("nginx").reload(ctx)
