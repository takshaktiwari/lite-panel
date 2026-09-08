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

    with session_scope() as db:
        site = db.get(Site, site_id)
        if site is None:
            raise JobFailed("That site no longer exists.")
        try:
            sites_service.delete_site(db, ctx, site, remove_files=remove_files)
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
    password = ctx.payload["password"]
    site_id = ctx.payload.get("site_id")

    ctx.log(f"Creating database {name}")
    try:
        db_service.create_database(name, user, password)
    except (ValidationError, RuntimeError) as exc:
        raise _fail(exc) from exc

    with session_scope() as db:
        db.add(SiteDatabase(db_name=name, db_user=user, site_id=site_id))

    ctx.log(f"Database {name} created and granted to {user}")


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


# --------------------------------------------------------------------------
# FTP
# --------------------------------------------------------------------------


@register("ftp.enable")
def enable_ftp(ctx) -> None:
    with session_scope() as db:
        site = db.get(Site, ctx.payload["site_id"])
        if site is None:
            raise JobFailed("That site no longer exists.")
        try:
            ftp_service.enable_ftp(db, ctx, site, ctx.payload["password"])
        except (ValidationError, RuntimeError) as exc:
            raise _fail(exc) from exc


@register("ftp.disable")
def disable_ftp(ctx) -> None:
    with session_scope() as db:
        account = db.get(FtpAccount, ctx.payload["account_id"])
        if account is None:
            raise JobFailed("That FTP account no longer exists.")
        ftp_service.disable_ftp(db, ctx, account)


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
# Cron
# --------------------------------------------------------------------------


@register("cron.sync")
def sync_cron(ctx) -> None:
    """Install a site's crontab from what is currently in the database.

    Runs after every create/edit/toggle/delete in the cron router -- the
    database row is already committed by then, so this only ever has to
    reflect it onto disk, never decide what belongs there.
    """
    with session_scope() as db:
        site = db.get(Site, ctx.payload["site_id"])
        if site is None:
            raise JobFailed("That site no longer exists.")
        jobs = db.scalars(select(CronJob).where(CronJob.site_id == site.id)).all()
        try:
            cron_service.apply_crontab(ctx, site.system_user, jobs)
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
