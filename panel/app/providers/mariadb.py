"""MariaDB.

The panel connects as root over the unix socket, which is how Debian and
Ubuntu ship MariaDB by default.  That is a deliberate choice: it means no
database root password exists anywhere on disk for an attacker to find, and
none has to be stored in the panel's own database.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from app.providers.base import Provider, register
from app.services import apt

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("/etc/mysql/conf.d/99-lite-panel.cnf")
SOCKET_CANDIDATES = (
    "/var/run/mysqld/mysqld.sock",
    "/run/mysqld/mysqld.sock",
    "/tmp/mysql.sock",
)


@register
class MariaDbProvider(Provider):
    key = "mariadb"
    name = "MariaDB"
    description = "Database server, with Adminer for browsing and editing."
    service_name = "mariadb"

    def installed_versions(self) -> List[str]:
        version = apt.installed_version("mariadb-server")
        return [version] if version else []

    def install(self, ctx, version: Optional[str] = None) -> None:
        if self.is_installed():
            ctx.log("MariaDB is already installed")
        else:
            ctx.log("Installing MariaDB")
            apt.install(ctx, ["mariadb-server", "mariadb-client"])

        self.render_config(ctx)
        self.enable_service(ctx)

        # Restart rather than reload: the tuning drop-in only takes effect on
        # a full restart, and on a small box the buffer-pool size in it is the
        # difference between starting and being OOM-killed.
        ctx.check(["systemctl", "restart", "mariadb"], timeout=180)

        from app.services import databases as db_service

        try:
            db_service.ensure_adminer_account()
            ctx.log("Adminer login account ready")
        except Exception as exc:  # noqa: BLE001 - Adminer working is not install-critical
            ctx.log(f"note: could not set up Adminer's login account: {exc}")

        ctx.log("MariaDB ready")

    def uninstall(self, ctx, version: Optional[str] = None) -> None:
        from sqlalchemy import select

        from app.database import session_scope
        from app.models import SiteDatabase

        with session_scope() as db:
            count = len(db.scalars(select(SiteDatabase)).all())
        if count:
            raise RuntimeError(
                f"{count} database(s) are still managed by the panel. "
                "Delete them first — removing MariaDB would destroy their data."
            )

        ctx.log("Removing MariaDB")
        ctx.run(["systemctl", "disable", "--now", "mariadb"])
        # Not purged: purging drops /var/lib/mysql, and an operator removing
        # the package should not silently lose databases the panel never knew
        # about.
        apt.remove(ctx, ["mariadb-server", "mariadb-client"])
        ctx.log("MariaDB removed; data in /var/lib/mysql was left in place")

    def render_config(self, ctx=None) -> None:
        from app.services import renderer, tuning

        if not CONFIG_PATH.parent.is_dir():
            if ctx:
                ctx.log(f"skipping tuning: {CONFIG_PATH.parent} does not exist")
            return

        renderer.render_to_file(
            "mariadb.cnf.j2",
            CONFIG_PATH,
            {"settings": tuning.mariadb_settings()},
        )
        if ctx:
            ctx.log(f"wrote {CONFIG_PATH}")

    @staticmethod
    def socket_path() -> Optional[str]:
        for candidate in SOCKET_CANDIDATES:
            if Path(candidate).exists():
                return candidate
        return None
