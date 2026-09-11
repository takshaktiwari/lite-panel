"""Job handlers.

Every privileged operation the panel performs is registered here, so there is
one place to see everything that can change the server.  Handlers run on the
job worker thread, take their arguments from ``ctx.payload``, and report
progress with ``ctx.log``.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.database import session_scope
from app.jobs import JobFailed, register
from app.models import CronJob, FtpAccount, InstalledProvider, Site
from app.providers import get_provider
from app.services import cron as cron_service
from app.services import databases as db_service
from app.services import files as files_service
from app.services import ftp as ftp_service
from app.services import renderer
from app.services import sites as sites_service
from app.validators import ValidationError

logger = logging.getLogger(__name__)


def _fail(exc: Exception) -> "JobFailed":
    """Turn an expected error into a readable job failure."""
    return JobFailed(str(exc))


# --------------------------------------------------------------------------
# Stack
# --------------------------------------------------------------------------


@register("provider.install")
def install_provider(ctx) -> None:
    key = ctx.payload["key"]
    version = ctx.payload.get("version")
    provider = get_provider(key)

    try:
        provider.install(ctx, version)
    except ValidationError as exc:
        raise _fail(exc) from exc

    with session_scope() as db:
        existing = db.scalar(
            select(InstalledProvider).where(
                InstalledProvider.key == key,
                InstalledProvider.version == (version or ""),
            )
        )
        if existing is None:
            db.add(InstalledProvider(key=key, version=version or ""))


@register("provider.uninstall")
def uninstall_provider(ctx) -> None:
    key = ctx.payload["key"]
    version = ctx.payload.get("version")
    provider = get_provider(key)

    try:
        provider.uninstall(ctx, version)
    except (ValidationError, RuntimeError) as exc:
        raise _fail(exc) from exc

    with session_scope() as db:
        row = db.scalar(
            select(InstalledProvider).where(
                InstalledProvider.key == key,
                InstalledProvider.version == (version or ""),
            )
        )
        if row is not None:
            db.delete(row)


@register("php.extensions")
def change_php_extensions(ctx) -> None:
    version = ctx.payload["version"]
    add = ctx.payload.get("add") or []
    remove = ctx.payload.get("remove") or []
    php = get_provider("php")

    try:
        if remove:
            ctx.log(f"Removing {len(remove)} extension(s) from PHP {version}")
            php.remove_extensions(ctx, version, remove)
        if add:
            ctx.log(f"Installing {len(add)} extension(s) for PHP {version}")
            php.install_extensions(ctx, version, add)
        if not add and not remove:
            ctx.log("nothing to change")
    except ValidationError as exc:
        raise _fail(exc) from exc


@register("php.add_repository")
def add_php_repository(ctx) -> None:
    try:
        get_provider("php").add_repository(ctx)
    except (ValidationError, RuntimeError) as exc:
        raise _fail(exc) from exc


# --------------------------------------------------------------------------
# Sites
# --------------------------------------------------------------------------


@register("site.create")
def create_site(ctx) -> None:
    payload = ctx.payload
    with session_scope() as db:
        try:
            site = sites_service.create_site(
                db,
                ctx,
                name=payload["name"],
                domain=payload["domain"],
                php_version=payload.get("php_version") or None,
                subfolder=payload.get("subfolder", ""),
                redirect_www=bool(payload.get("redirect_www", True)),
            )
        except (ValidationError, RuntimeError) as exc:
            raise _fail(exc) from exc

        if payload.get("enable_ssl"):
            _try_enable_ssl_at_creation(db, ctx, site)


def _try_enable_ssl_at_creation(db, ctx, site) -> None:
    """Best-effort HTTPS right after creating a site.

    Never fails the site.create job: issuing a certificate needs the
    domain's DNS to already point here, which is frequently not true yet at
    the moment a site is created. The site itself is created and works over
    plain HTTP either way -- this just saves a manual "Enable SSL" click on
    the site detail page for the common case where DNS is already correct.
    """
    certbot = get_provider("certbot")
    if not certbot.is_installed():
        ctx.log(
            "HTTPS was requested but certbot isn't installed -- install it from "
            "the Stack page, then use \"Enable SSL\" on this site."
        )
        return
    try:
        sites_service.enable_ssl(db, ctx, site)
    except (ValidationError, RuntimeError) as exc:
        ctx.log(
            f"warning: could not enable HTTPS yet ({exc}). The site is live over "
            "HTTP -- make sure DNS points here, then use \"Enable SSL\" on the "
            "site detail page to retry."
        )


@register("site.delete")
def delete_site(ctx) -> None:
    site_id = ctx.payload["site_id"]
    remove_files = bool(ctx.payload.get("remove_files"))
    delete_databases = bool(ctx.payload.get("delete_databases"))

    with session_scope() as db:
        site = db.get(Site, site_id)
        if site is None:
            raise JobFailed("That site no longer exists.")
        try:
            sites_service.delete_site(
                db, ctx, site, remove_files=remove_files, delete_databases=delete_databases
            )
        except (ValidationError, RuntimeError) as exc:
            raise _fail(exc) from exc


@register("site.php_version")
def change_site_php(ctx) -> None:
    with session_scope() as db:
        site = db.get(Site, ctx.payload["site_id"])
        if site is None:
            raise JobFailed("That site no longer exists.")
        try:
            sites_service.set_php_version(db, ctx, site, ctx.payload.get("php_version") or None)
        except (ValidationError, RuntimeError) as exc:
            raise _fail(exc) from exc


@register("site.webroot")
def change_site_webroot(ctx) -> None:
    with session_scope() as db:
        site = db.get(Site, ctx.payload["site_id"])
        if site is None:
            raise JobFailed("That site no longer exists.")
        try:
            sites_service.set_webroot(db, ctx, site, ctx.payload.get("webroot", ""))
        except (ValidationError, RuntimeError) as exc:
            raise _fail(exc) from exc


@register("site.ssl_enable")
def enable_site_ssl(ctx) -> None:
    with session_scope() as db:
        site = db.get(Site, ctx.payload["site_id"])
        if site is None:
            raise JobFailed("That site no longer exists.")
        try:
            sites_service.enable_ssl(
                db,
                ctx,
                site,
                email=ctx.payload.get("email") or None,
                staging=bool(ctx.payload.get("staging")),
            )
        except (ValidationError, RuntimeError) as exc:
            raise _fail(exc) from exc


@register("site.ssl_disable")
def disable_site_ssl(ctx) -> None:
    with session_scope() as db:
        site = db.get(Site, ctx.payload["site_id"])
        if site is None:
            raise JobFailed("That site no longer exists.")
        sites_service.disable_ssl(db, ctx, site)


# --------------------------------------------------------------------------
# Databases
# --------------------------------------------------------------------------


@register("database.create")
def create_database(ctx) -> None:
    from app.models import SiteDatabase

    name = ctx.payload["db_name"]
    user = ctx.payload["db_user"]
    password = ctx.payload.get("password") or None
    site_id = ctx.payload.get("site_id")

    ctx.log(f"Creating database {name}")
    try:
        db_service.create_database(name, user, password)
    except (ValidationError, RuntimeError) as exc:
        raise _fail(exc) from exc

    with session_scope() as db:
        db.add(SiteDatabase(db_name=name, db_user=user, site_id=site_id))

    ctx.log(f"Database {name} created and granted to {user}")


@register("database.update_user")
def update_database_user(ctx) -> None:
    from app.models import SiteDatabase

    database_id = ctx.payload["database_id"]
    new_user = ctx.payload["db_user"]
    password = ctx.payload.get("password") or None

    with session_scope() as db:
        record = db.get(SiteDatabase, database_id)
        if record is None:
            raise JobFailed("That database record no longer exists.")
        db_name = record.db_name
        old_user = record.db_user

        ctx.log(f"Reassigning database {db_name} from user '{old_user}' to '{new_user}'")
        try:
            db_service.reassign_database_user(db_name, new_user, old_user=old_user, new_password=password)
        except (ValidationError, RuntimeError) as exc:
            raise _fail(exc) from exc

        record.db_user = new_user
        db.add(record)

    ctx.log(f"Database {db_name} user updated to {new_user}")


@register("database.delete")
def delete_database(ctx) -> None:
    from app.models import SiteDatabase

    database_id = ctx.payload["database_id"]

    with session_scope() as db:
        record = db.get(SiteDatabase, database_id)
        if record is None:
            raise JobFailed("That database record no longer exists.")
        name, user = record.db_name, record.db_user

        ctx.log(f"Dropping database {name}")
        try:
            db_service.drop_database(name, user)
        except (ValidationError, RuntimeError) as exc:
            raise _fail(exc) from exc

        db.delete(record)

    ctx.log(f"Database {name} dropped")


@register("database.password")
def change_database_password(ctx) -> None:
    try:
        db_service.change_password(ctx.payload["db_user"], ctx.payload["password"])
    except (ValidationError, RuntimeError) as exc:
        raise _fail(exc) from exc
    ctx.log(f"password changed for {ctx.payload['db_user']}")


@register("database.import")
def import_database_task(ctx) -> None:
    import os
    db_name = ctx.payload["db_name"]
    source_file = ctx.payload["source_file"]
    cleanup = ctx.payload.get("cleanup", True)

    ctx.log(f"Importing database dump into {db_name}")
    try:
        db_service.import_database(db_name, source_file, ctx=ctx)
    except (ValidationError, RuntimeError) as exc:
        raise _fail(exc) from exc
    finally:
        if cleanup and os.path.exists(source_file):
            try:
                os.unlink(source_file)
            except OSError:
                pass

    ctx.log(f"Database dump successfully imported into {db_name}")


# --------------------------------------------------------------------------
# FTP
# --------------------------------------------------------------------------


@register("ftp.create")
def create_ftp_account(ctx) -> None:
    with session_scope() as db:
        try:
            ftp_service.create_account(
                db,
                ctx,
                username=ctx.payload["username"],
                password=ctx.payload["password"],
                path=ctx.payload["path"],
            )
        except (ValidationError, RuntimeError) as exc:
            raise _fail(exc) from exc


@register("ftp.delete")
def delete_ftp_account(ctx) -> None:
    with session_scope() as db:
        account = db.get(FtpAccount, ctx.payload["account_id"])
        if account is None:
            raise JobFailed("That FTP account no longer exists.")
        ftp_service.delete_account(db, ctx, account)


@register("ftp.password")
def change_ftp_password(ctx) -> None:
    with session_scope() as db:
        account = db.get(FtpAccount, ctx.payload["account_id"])
        if account is None:
            raise JobFailed("That FTP account no longer exists.")
        try:
            ftp_service.set_ftp_password(ctx, account, ctx.payload["password"])
        except ValidationError as exc:
            raise _fail(exc) from exc


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


@register("files.extract")
def extract_archive(ctx) -> None:
    """Unpack a zip in the file manager.

    Routed through a job rather than done inline in the request because a
    large archive can take long enough that a synchronous POST would time
    out -- and it lets the browser show a progress bar via ctx.progress().
    """
    try:
        files_service.extract_archive(ctx.payload["target"], ctx)
    except ValidationError as exc:
        raise _fail(exc) from exc


# --------------------------------------------------------------------------
# Cron
# --------------------------------------------------------------------------


@register("cron.sync")
def sync_cron(ctx) -> None:
    """Install a crontab from what is currently in the database, for either
    one site's own system user or (``site_id`` null) root's own.

    Runs after every create/edit/toggle/delete in the cron router -- the
    database row is already committed by then, so this only ever has to
    reflect it onto disk, never decide what belongs there.
    """
    site_id = ctx.payload.get("site_id")
    with session_scope() as db:
        if site_id is None:
            username = "root"
            jobs = db.scalars(select(CronJob).where(CronJob.site_id.is_(None))).all()
        else:
            site = db.get(Site, site_id)
            if site is None:
                raise JobFailed("That site no longer exists.")
            username = site.system_user
            jobs = db.scalars(select(CronJob).where(CronJob.site_id == site.id)).all()

        try:
            cron_service.apply_crontab(ctx, username, jobs)
        except ValidationError as exc:
            raise _fail(exc) from exc


# --------------------------------------------------------------------------
# Maintenance
# --------------------------------------------------------------------------


@register("panel.rebuild")
def rebuild(ctx) -> None:
    """Re-render every panel-owned config file from the database."""
    ctx.log("Rebuilding all panel-managed configuration")
    renderer.rebuild_all(ctx)

    nginx = get_provider("nginx")
    if nginx.is_installed():
        nginx.reload(ctx)
    ctx.log("Rebuild complete")


@register("setup.bootstrap")
def bootstrap(ctx) -> None:
    """First-run provisioning, driven by the setup wizard's choices.

    Runs as one job so the operator watches a single stream of output rather
    than a queue of separate installs.
    """
    payload = ctx.payload
    php_versions = payload.get("php_versions") or []

    steps = [("nginx", None)]
    steps += [("php", version) for version in php_versions]
    if payload.get("mariadb"):
        steps.append(("mariadb", None))
    if payload.get("certbot"):
        steps.append(("certbot", None))
    if payload.get("vsftpd"):
        steps.append(("vsftpd", None))

    ctx.log(f"Provisioning {len(steps)} component(s)")

    for key, version in steps:
        label = f"{key} {version}".strip()
        ctx.log("")
        ctx.log(f"=== {label} ===")
        try:
            get_provider(key).install(ctx, version)
        except (ValidationError, RuntimeError) as exc:
            # Keep going: a failed optional component should not stop the
            # rest of the stack from being installed.
            ctx.log(f"warning: {label} failed: {exc}")
            continue

        with session_scope() as db:
            exists = db.scalar(
                select(InstalledProvider).where(
                    InstalledProvider.key == key,
                    InstalledProvider.version == (version or ""),
                )
            )
            if exists is None:
                db.add(InstalledProvider(key=key, version=version or ""))

    if php_versions:
        with session_scope() as db:
            default = db.scalar(
                select(InstalledProvider).where(
                    InstalledProvider.key == "php",
                    InstalledProvider.version == php_versions[0],
                )
            )
            if default is not None:
                default.is_default = True

    ctx.log("")
    ctx.log("Setup complete.")


@register("panel.update")
def update_panel(ctx) -> None:
    """Self-update Lite-Panel from GitHub.

    1. Fetches latest git tags and commits from origin.
    2. Checks out the requested release tag (or latest origin/main).
    3. Upgrades python requirements in the panel's virtualenv.
    4. Schedules a service restart via systemd or detached nohup.
    """
    from app.config import get_settings
    from app.services.version import clear_cache

    settings = get_settings()
    install_dir = settings.install_dir
    target_tag = ctx.payload.get("tag")

    ctx.log(f"Starting Lite-Panel update (target: {target_tag or 'latest'})")

    if not install_dir.is_dir():
        raise _fail(RuntimeError(f"Install directory {install_dir} does not exist."))

    # Configure git safe directory so permissions from rsync/custom users don't trigger fatal error
    ctx.run(["git", "config", "--global", "--add", "safe.directory", str(install_dir)])

    git_dir = install_dir / ".git"
    if not git_dir.is_dir():
        ctx.log("Git repository metadata not found in install directory. Initializing...")
        ctx.check(["git", "init"], cwd=install_dir)

    # Ensure remote 'origin' is configured
    remotes_check = ctx.run(["git", "remote", "get-url", "origin"], cwd=install_dir)
    if remotes_check != 0:
        ctx.log("Setting remote origin to https://github.com/takshaktiwari/lite-panel.git")
        ctx.check(["git", "remote", "add", "origin", "https://github.com/takshaktiwari/lite-panel.git"], cwd=install_dir)
    else:
        ctx.run(["git", "remote", "set-url", "origin", "https://github.com/takshaktiwari/lite-panel.git"], cwd=install_dir)

    ctx.log(f"Fetching updates at {install_dir}...")

    # 1. Fetch tags and branches
    ctx.check(["git", "fetch", "--tags", "origin"], cwd=install_dir)



    # Checkout and align working tree with target tag or origin/main
    if target_tag:
        ctx.log(f"Checking out release tag: {target_tag}")
        ctx.check(["git", "checkout", "-f", target_tag], cwd=install_dir)
        ctx.check(["git", "reset", "--hard", target_tag], cwd=install_dir)
    else:
        ctx.log("Updating to latest origin/main")
        ctx.check(["git", "checkout", "-B", "main", "origin/main"], cwd=install_dir)
        ctx.check(["git", "reset", "--hard", "origin/main"], cwd=install_dir)


    # 3. Update dependencies
    venv_pip = install_dir / "venv" / "bin" / "pip"
    req_file = install_dir / "panel" / "requirements.txt"
    if venv_pip.is_file() and req_file.is_file():
        ctx.log("Updating Python dependencies...")
        ctx.check([str(venv_pip), "install", "--quiet", "-r", str(req_file)])

    # Clear version cache so the updated version immediately reflects
    clear_cache()

    ctx.log("")
    ctx.log("Lite-Panel code and dependencies updated successfully.")
    ctx.log("Scheduling service restart...")

    # Flush all output to db before scheduling restart
    ctx.flush()

    # 4. Schedule delayed restart so this job finishes and flushes cleanly
    try:
        from app.shell import run
        # Use nohup + sleep 2 so the HTTP connection and job status flush first
        run(
            ["nohup", "sh", "-c", "sleep 2 && systemctl restart lite-panel.service >/dev/null 2>&1 &"],
            check=False,
            timeout=5,
        )
        ctx.log("Service restart scheduled in 2 seconds.")
    except Exception as exc:  # noqa: BLE001
        ctx.log(f"Note: Could not automatically restart service: {exc}")

