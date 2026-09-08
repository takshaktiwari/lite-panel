"""FTP accounts.

Two kinds, chosen automatically by where the requested path lives -- see
FtpAccount's docstring in app.models. Ownership is never shared between two
accounts: a folder that already belongs to a site, or to another FTP
account, refuses a second one rather than reconciling ACLs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.models import FtpAccount, Site
from app.providers import get_provider
from app.services import sites as sites_service
from app.shell import run
from app.validators import ValidationError, resolve_within, validate_system_username

logger = logging.getLogger(__name__)

SHELLS_FILE = Path("/etc/shells")


def ensure_shell_allowed(shell: str) -> None:
    """List a shell in /etc/shells.

    vsftpd authenticates through PAM, which rejects any account whose shell
    is absent from this file. Site accounts use ``nologin``; dedicated
    FTP-only accounts use ``sites_service.FTP_SHELL`` -- neither can open an
    interactive session, but PAM still needs to see the shell listed.
    """
    try:
        existing = SHELLS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        existing = []

    if shell in [line.strip() for line in existing]:
        return

    with SHELLS_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"{shell}\n")
    logger.info("added %s to /etc/shells", shell)


# --------------------------------------------------------------------------
# Path resolution and ownership conflicts
# --------------------------------------------------------------------------


def resolve_path(candidate: str) -> Path:
    """Validate a chosen path stays within the panel's managed sites root --
    the same boundary the file manager enforces, so an FTP account can never
    be pointed outside it."""
    return resolve_within(sites_service.sites_root(), candidate)


def site_for_path(db: OrmSession, path: Path) -> Optional[Site]:
    """The site whose root directory is exactly ``path``, if any."""
    return db.scalar(select(Site).where(Site.root_dir == str(path)))


def _is_within(path: Path, ancestor: Path) -> bool:
    try:
        path.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _conflicting_account(db: OrmSession, path: Path) -> Optional[FtpAccount]:
    """An existing account whose folder is ``path`` itself, contains it, or
    sits inside it -- any of which would mean two accounts owning
    overlapping territory."""
    for account in db.scalars(select(FtpAccount)).all():
        existing = Path(account.home_dir)
        if existing == path or _is_within(path, existing) or _is_within(existing, path):
            return account
    return None


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------


def create_account(db: OrmSession, ctx, *, username: str, password: str, path: str) -> FtpAccount:
    """Create an FTP account for ``path``.

    If ``path`` is exactly an existing site's root directory, the account
    *is* that site's own system user -- ``username`` is ignored, since that
    identity already exists and already owns the folder. Otherwise a new,
    dedicated system user named ``username`` is created and given outright
    ownership of ``path``.
    """
    if not password:
        raise ValidationError("A password is required.")

    vsftpd = get_provider("vsftpd")
    if not vsftpd.is_installed():
        raise ValidationError("vsftpd is not installed. Install it from the Stack page.")

    resolved = resolve_path(path)
    if resolved == sites_service.sites_root():
        raise ValidationError("Choose a specific site or folder, not the whole sites directory.")

    conflict = _conflicting_account(db, resolved)
    if conflict:
        raise ValidationError(
            f"'{conflict.home_dir}' already has an FTP account ({conflict.username}). "
            "Delete it first, or choose a different folder."
        )

    site = site_for_path(db, resolved)
    if site is not None:
        return _create_site_account(db, ctx, site, password)
    return _create_dedicated_account(db, ctx, username, password, resolved)


def _create_site_account(db: OrmSession, ctx, site: Site, password: str) -> FtpAccount:
    """Reuse a site's own system user as its FTP login -- ownership of
    root_dir is already correct, so nothing is chowned."""
    username = site.system_user
    ctx.log(f"Enabling FTP for {site.domain} as {username}")

    ensure_shell_allowed(sites_service.SITE_SHELL)
    _set_password(username, password)

    get_provider("vsftpd").write_user_config(username, site.root_dir)
    sites_service.add_to_ftp_userlist(username)
    ctx.check(["systemctl", "restart", "vsftpd"], timeout=60)

    account = FtpAccount(site_id=site.id, username=username, home_dir=site.root_dir)
    db.add(account)
    db.commit()

    ctx.log(f"FTP enabled for {username}")
    return account


def _create_dedicated_account(db: OrmSession, ctx, username: str, password: str, path: Path) -> FtpAccount:
    """Create a brand-new system user that owns ``path`` outright."""
    username = validate_system_username(username)
    if username.startswith("site_"):
        # Reserved for sites (see app.validators.site_username) -- a
        # dedicated account claiming that prefix could collide with a site
        # created later under the name it implies.
        raise ValidationError("Usernames starting with 'site_' are reserved for sites.")
    if db.scalar(select(Site).where(Site.system_user == username)):
        raise ValidationError(f"'{username}' is already a site's system account.")
    if db.scalar(select(FtpAccount).where(FtpAccount.username == username)):
        raise ValidationError(f"'{username}' is already in use by another FTP account.")

    ctx.log(f"Creating FTP account {username} for {path}")

    _create_ftp_system_user(ctx, username, path)
    path.mkdir(parents=True, exist_ok=True)
    run(["chown", "-R", f"{username}:{username}", str(path)])

    ensure_shell_allowed(sites_service.FTP_SHELL)
    _set_password(username, password)

    get_provider("vsftpd").write_user_config(username, str(path))
    sites_service.add_to_ftp_userlist(username)
    ctx.check(["systemctl", "restart", "vsftpd"], timeout=60)

    account = FtpAccount(site_id=None, username=username, home_dir=str(path))
    db.add(account)
    db.commit()

    ctx.log(f"FTP account {username} ready at {path}")
    return account


def _create_ftp_system_user(ctx, username: str, home: Path) -> None:
    if sites_service._user_exists(username):
        raise ValidationError(f"System account '{username}' already exists.")
    ctx.check(
        [
            "useradd",
            "--create-home",
            "--home-dir", str(home),
            "--shell", sites_service.FTP_SHELL,
            # Its own private group -- this account owns exactly one folder
            # and shares it with nobody.
            "--user-group",
            username,
        ]
    )
    ctx.log(f"created system user {username}")


# --------------------------------------------------------------------------
# Password / deletion
# --------------------------------------------------------------------------


def set_ftp_password(ctx, account: FtpAccount, password: str) -> None:
    if not password:
        raise ValidationError("A password is required.")
    _set_password(account.username, password)
    ctx.log(f"password changed for {account.username}")


def delete_account(db: OrmSession, ctx, account: FtpAccount) -> None:
    """Revoke an FTP account. Never touches its folder.

    A site-linked account's system user stays -- it still runs PHP-FPM for
    that site -- so only its ability to log in over FTP is removed. A
    dedicated account's system user is removed too (nothing else needs it),
    but with a plain ``userdel``, never ``--remove``: that flag is what
    deletes the home directory along with the account, and a folder full of
    a site's or an operator's files must survive deleting the login that
    happened to point at it.
    """
    username = account.username
    ctx.log(f"Removing FTP account {username}")

    get_provider("vsftpd").remove_user_config(username)
    sites_service._remove_from_ftp_userlist(username)

    if account.site_id is not None:
        _lock_password(username)
    else:
        ctx.run(["userdel", username])

    db.delete(account)
    db.commit()

    ctx.run(["systemctl", "restart", "vsftpd"])
    ctx.log(f"FTP account {username} removed; files at {account.home_dir} were left in place")


def _set_password(username: str, password: str) -> None:
    """Set a system password.

    The value goes over stdin, never in an argument: arguments are visible to
    every user on the machine through ``ps`` for as long as the process runs.
    """
    run(["chpasswd"], input=f"{username}:{password}\n")


def _lock_password(username: str) -> None:
    run(["passwd", "--lock", username], check=False)
