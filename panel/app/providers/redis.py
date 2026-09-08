"""Redis -- in-memory store for caching, sessions and queues."""

from __future__ import annotations

from typing import List, Optional

from app.providers.base import Provider, register
from app.services import apt

PACKAGE = "redis-server"


@register
class RedisProvider(Provider):
    key = "redis"
    name = "Redis"
    description = "In-memory key-value store, for caching, sessions and queues."
    category = "tools"
    service_name = PACKAGE

    def installed_versions(self) -> List[str]:
        version = apt.installed_version(PACKAGE)
        return [version] if version else []

    def install(self, ctx, version: Optional[str] = None) -> None:
        if self.is_installed():
            ctx.log("Redis is already installed")
        else:
            ctx.log("Installing Redis")
            apt.install(ctx, [PACKAGE])
        self.enable_service(ctx)
        ctx.log("Redis ready")

    def uninstall(self, ctx, version: Optional[str] = None) -> None:
        ctx.log("Removing Redis")
        ctx.run(["systemctl", "disable", "--now", PACKAGE])
        apt.remove(ctx, [PACKAGE])
        ctx.log("Redis removed")
