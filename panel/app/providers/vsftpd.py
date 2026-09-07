"""FTP via vsftpd.

Each site's own system user *is* its FTP login, which is why there is no ACL
reconciliation anywhere here: the account that uploads the files is the account
that runs PHP for that site, so the files are already owned correctly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from app.providers.base import Provider, register
from app.services import apt, system
from app.validators import validate_system_username

logger = logging.getLogger(__name__)

MAIN_CONFIG = Path("/etc/vsftpd.conf")
USER_CONFIG_DIR = Path("/etc/vsftpd/user_conf")
PASSIVE_MIN_PORT = 30000
PASSIVE_MAX_PORT = 30100


@register
class VsftpdProvider(Provider):
    key = "vsftpd"
    name = "FTP (vsftpd)"
    description = "FTP access to site files, one login per site."
    service_name = "vsftpd"

    def installed_versions(self) -> List[str]:
        version = apt.installed_version("vsftpd")
        return [version] if version else []

    def install(self, ctx, version: Optional[str] = None) -> None:
        if self.is_installed():
            ctx.log("vsftpd is already installed")
        else:
            ctx.log("Installing vsftpd")
            apt.install(ctx, ["vsftpd"])

        USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.render_config(ctx)
        self.enable_service(ctx)
        ctx.check(["systemctl", "restart", "vsftpd"], timeout=60)

        ctx.log(
            f"vsftpd ready. Open ports 21 and {PASSIVE_MIN_PORT}-{PASSIVE_MAX_PORT} "
            "in your firewall or EC2 security group, or connections will hang "
            "after login."
        )

    def uninstall(self, ctx, version: Optional[str] = None) -> None:
        ctx.log("Removing vsftpd")
        ctx.run(["systemctl", "disable", "--now", "vsftpd"])
        apt.remove(ctx, ["vsftpd"])
        ctx.log("vsftpd removed; site system users were left in place")

    def render_config(self, ctx=None) -> None:
        """Write the whole vsftpd.conf from our template.

        The panel owns this file outright rather than patching lines in the
        distribution default, so the result is the same every time it runs and
        a bad edit can be fixed by re-rendering.
        """
        from app.services import renderer

        if not MAIN_CONFIG.parent.is_dir():
            return

        # Keep one copy of whatever was there before we first took over.
        backup = MAIN_CONFIG.with_suffix(".conf.pre-lite-panel")
        if MAIN_CONFIG.exists() and not backup.exists():
            backup.write_bytes(MAIN_CONFIG.read_bytes())
            if ctx:
                ctx.log(f"saved original config to {backup}")

        renderer.render_to_file(
            "vsftpd.conf.j2",
            MAIN_CONFIG,
            {
                "user_config_dir": str(USER_CONFIG_DIR),
                "passive_min": PASSIVE_MIN_PORT,
                "passive_max": PASSIVE_MAX_PORT,
                # Passive mode advertises an address to the client. Behind NAT
                # (which is every EC2 instance) that must be the public IP, or
                # the client is told to connect to an unroutable one.
                "public_ip": system.public_ip(),
            },
        )
        if ctx:
            ctx.log(f"wrote {MAIN_CONFIG}")

    # -- per-user config ---------------------------------------------------

    def user_config_path(self, username: str) -> Path:
        return USER_CONFIG_DIR / validate_system_username(username)

    def write_user_config(self, username: str, home_dir: str) -> None:
        from app.services import renderer

        USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        renderer.render_to_file(
            "vsftpd-user.conf.j2",
            self.user_config_path(username),
            {"home_dir": home_dir},
        )

    def remove_user_config(self, username: str) -> None:
        path = self.user_config_path(username)
        if path.exists():
            path.unlink()
