"""Nginx: the web server that fronts both hosted sites and the panel itself."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from app.providers.base import Provider, register
from app.services import apt
from app.shell import CommandError, run

logger = logging.getLogger(__name__)

SITES_AVAILABLE = Path("/etc/nginx/sites-available")
SITES_ENABLED = Path("/etc/nginx/sites-enabled")

# Every file the panel writes carries this prefix, so it can tell its own
# config apart from anything the operator put there by hand and never has to
# guess whether a file is safe to overwrite.
PANEL_PREFIX = "lite-panel-"


@register
class NginxProvider(Provider):
    key = "nginx"
    name = "Nginx"
    description = "Web server. Serves your sites and the panel itself."
    service_name = "nginx"

    def installed_versions(self) -> List[str]:
        version = apt.installed_version("nginx")
        if version:
            return [version]
        return ["installed"] if apt.is_installed("nginx-core") else []

    def install(self, ctx, version: Optional[str] = None) -> None:
        if self.is_installed():
            ctx.log("Nginx is already installed")
        else:
            ctx.log("Installing Nginx")
            apt.install(ctx, ["nginx"])

        SITES_AVAILABLE.mkdir(parents=True, exist_ok=True)
        SITES_ENABLED.mkdir(parents=True, exist_ok=True)
        self.enable_service(ctx)
        ctx.log("Nginx ready")

    def uninstall(self, ctx, version: Optional[str] = None) -> None:
        raise RuntimeError(
            "Nginx cannot be removed: the panel is served through it, so "
            "removing it would take the panel offline with no way back in."
        )

    # -- config ------------------------------------------------------------

    def config_path(self, name: str) -> Path:
        return SITES_AVAILABLE / f"{PANEL_PREFIX}{name}.conf"

    def enabled_path(self, name: str) -> Path:
        return SITES_ENABLED / f"{PANEL_PREFIX}{name}.conf"

    def enable_site(self, name: str) -> None:
        """Symlink a site into sites-enabled (idempotent)."""
        target, link = self.config_path(name), self.enabled_path(name)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)

    def disable_site(self, name: str) -> None:
        link = self.enabled_path(name)
        if link.is_symlink() or link.exists():
            link.unlink()

    def remove_site(self, name: str) -> None:
        self.disable_site(name)
        config = self.config_path(name)
        if config.exists():
            config.unlink()

    def test_config(self) -> tuple:
        """Run ``nginx -t``.  Returns (ok, output)."""
        try:
            result = run(["nginx", "-t"], check=False, timeout=30)
        except (CommandError, OSError) as exc:
            return False, str(exc)
        return result.ok, (result.stderr or result.stdout).strip()

    def reload(self, ctx=None) -> None:
        """Validate then reload.

        Testing first matters: ``systemctl reload`` on a broken config leaves
        the old one running but reports success, so a later unrelated restart
        would be the thing that takes every site down.
        """
        ok, output = self.test_config()
        if ctx:
            ctx.log(output or "nginx -t passed")
        if not ok:
            raise RuntimeError(f"nginx configuration test failed:\n{output}")

        if ctx:
            ctx.check(["systemctl", "reload", "nginx"], timeout=60)
        else:
            run(["systemctl", "reload", "nginx"], timeout=60)
