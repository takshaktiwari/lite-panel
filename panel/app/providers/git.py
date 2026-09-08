"""Git -- version control, for cloning and deploying site repositories."""

from __future__ import annotations

from typing import List, Optional

from app.providers.base import Provider, register
from app.services import apt


@register
class GitProvider(Provider):
    key = "git"
    name = "Git"
    description = "Version control, for cloning and deploying site repositories."
    category = "tools"

    def installed_versions(self) -> List[str]:
        version = apt.installed_version("git")
        return [version] if version else []

    def install(self, ctx, version: Optional[str] = None) -> None:
        if self.is_installed():
            ctx.log("Git is already installed")
            return
        ctx.log("Installing Git")
        apt.install(ctx, ["git"])
        ctx.log("Git installed")

    def uninstall(self, ctx, version: Optional[str] = None) -> None:
        ctx.log("Removing Git")
        apt.remove(ctx, ["git"])
        ctx.log("Git removed")
