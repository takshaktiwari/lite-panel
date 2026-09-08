"""unzip -- extracting .zip archives on the command line.

Distinct from the file manager's own zip/unzip (app.services.files), which
shells out to nowhere and uses Python's zipfile module instead; this is for
everything else on the box that expects the ``unzip`` binary -- deploy
scripts, WP-CLI, build tooling.
"""

from __future__ import annotations

from typing import List, Optional

from app.providers.base import Provider, register
from app.services import apt


@register
class UnzipProvider(Provider):
    key = "unzip"
    name = "unzip"
    description = "Command-line archive extraction, used by deploy scripts and build tools."
    category = "tools"

    def installed_versions(self) -> List[str]:
        version = apt.installed_version("unzip")
        return [version] if version else []

    def install(self, ctx, version: Optional[str] = None) -> None:
        if self.is_installed():
            ctx.log("unzip is already installed")
            return
        ctx.log("Installing unzip")
        apt.install(ctx, ["unzip"])
        ctx.log("unzip installed")

    def uninstall(self, ctx, version: Optional[str] = None) -> None:
        ctx.log("Removing unzip")
        apt.remove(ctx, ["unzip"])
        ctx.log("unzip removed")
