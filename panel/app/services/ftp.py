"""FTP accounts.

An FTP account is not a separate identity: it is the site's own system user,
given a password.  That is why nothing here touches ACLs -- the account that
uploads a file is the account PHP runs as, so ownership is already right.
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
from app.validators import ValidationError

logger = logging.getLogger(__name__)

SHELLS_FILE = Path("/etc/shells")


def ensure_shell_allowed(shell: str = sites_service.SITE_SHELL) -> None:
    """List the site shell in /etc/shells.

    vsftpd authenticates through PAM, which rejects any account whose shell is
    absent from this file.  Site accounts use ``nologin`` so they cannot open
    an interactive session; listing it here lets FTP work without granting one.
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


def enable_ftp(db: OrmSession, ctx, site: Site, password: str) -> FtpAccount:
    """Give a site's system user an FTP password and let it log in."""
    if not password:
        raise ValidationError("A password is required.")

    vsftpd = get_provider("vsftpd")
    if not vsftpd.is_installed():
        raise ValidationError("vsftpd is not installed. Install it from the Stack page.")

    username = site.system_user
    ctx.log(f"Enabling FTP for {site.domain} as {username}")

    ensure_shell_allowed()
    _set_password(username, password)

    vsftpd.write_user_config(username, site.root_dir)
    sites_service.add_to_ftp_userlist(username)
    ctx.check(["systemctl", "restart", "vsftpd"], timeout=60)

    account = db.scalar(select(FtpAccount).where(FtpAccount.username == username))
    if account is None:
        account = FtpAccount(site_id=site.id, username=username, home_dir=site.root_dir)
        db.add(account)
    account.is_enabled = True
    db.commit()

    ctx.log(f"FTP enabled for {username}")
    return account


def set_ftp_password(ctx, account: FtpAccount, password: str) -> None:
    if not password:
        raise ValidationError("A password is required.")
    _set_password(account.username, password)
    ctx.log(f"password changed for {account.username}")


def disable_ftp(db: OrmSession, ctx, account: FtpAccount) -> None:
    """Revoke FTP access without deleting the account or its files.

    The system user has to stay: it owns the site's files and runs its PHP
    workers.  Only the ability to log in over FTP is removed.
    """
    username = account.username
    ctx.log(f"Disabling FTP for {username}")

    get_provider("vsftpd").remove_user_config(username)
    sites_service._remove_from_ftp_userlist(username)
    _lock_password(username)

    db.delete(account)
    db.commit()

    ctx.run(["systemctl", "restart", "vsftpd"])
    ctx.log(f"FTP disabled for {username}")


def _set_password(username: str, password: str) -> None:
    """Set a system password.

    The value goes over stdin, never in an argument: arguments are visible to
    every user on the machine through ``ps`` for as long as the process runs.
    """
    run(["chpasswd"], input=f"{username}:{password}\n")


def _lock_password(username: str) -> None:
    run(["passwd", "--lock", username], check=False)


def account_for(db: OrmSession, site: Site) -> Optional[FtpAccount]:
    return db.scalar(select(FtpAccount).where(FtpAccount.site_id == site.id))
