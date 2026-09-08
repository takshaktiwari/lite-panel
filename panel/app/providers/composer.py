"""Composer -- PHP's dependency manager.

Installed from the distro package rather than piping getcomposer.org's
installer script into ``php`` as root: apt already resolves the php-cli
dependency this needs, with nothing fetched from the network at install
time beyond what apt itself signs and verifies.
"""

from __future__ import annotations

from typing import List, Optional

from app.providers.base import Provider, register
from app.services import apt

PACKAGE = "composer"


@register
class ComposerProvider(Provider):
    key = "composer"
    name = "Composer"
    description = "PHP dependency manager."
    category = "tools"

    def installed_versions(self) -> List[str]:
        version = apt.installed_version(PACKAGE)
        return [version] if version else []

    def install(self, ctx, version: Optional[str] = None) -> None:
        if self.is_installed():
            ctx.log("Composer is already installed")
            return
        ctx.log("Installing Composer")
        apt.install(ctx, [PACKAGE])
        ctx.log("Composer installed")

    def uninstall(self, ctx, version: Optional[str] = None) -> None:
        ctx.log("Removing Composer")
        apt.remove(ctx, [PACKAGE])
        ctx.log("Composer removed")
