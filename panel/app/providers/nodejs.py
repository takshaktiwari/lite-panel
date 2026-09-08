"""Node.js -- installed from NodeSource, since the Debian/Ubuntu-shipped
``nodejs`` package lags far behind any actively maintained release line.

Unlike PHP, this is *not* several versions side by side: NodeSource's repo
serves exactly one major release line system-wide, so "installing" a
different major here means switching the whole box to it, the same as
running NodeSource's own setup script by hand. npm is bundled with the
``nodejs`` package -- there is no separate package to manage for it.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import List, Optional

from app.providers.base import Provider, register
from app.services import apt
from app.shell import CommandError, run
from app.validators import ValidationError, validate_node_major

logger = logging.getLogger(__name__)

PACKAGE = "nodejs"
SOURCE_LIST = Path("/etc/apt/sources.list.d/nodesource.list")

# Major release lines NodeSource currently publishes. Checked against a live
# probe before ever being offered or installed -- see is_major_supported --
# so a line that NodeSource has dropped support for quietly stops being
# offered rather than being installed and then failing.
NODE_MAJORS = ("18", "20", "22", "24")


def is_major_supported(major: str) -> bool:
    """Whether NodeSource actually serves a setup script for this major
    right now. A live probe, the same shape as apt.is_php_ppa_supported."""
    try:
        result = run(
            ["curl", "-fsSL", "--head", "--max-time", "5",
             f"https://deb.nodesource.com/setup_{major}.x"],
            check=False,
            timeout=10,
        )
        return result.returncode == 0
    except (CommandError, OSError):
        return False


@register
class NodeProvider(Provider):
    key = "nodejs"
    name = "Node.js"
    description = "JavaScript runtime, npm included. One version runs system-wide at a time."
    category = "tools"
    multi_version = True

    # -- discovery ---------------------------------------------------------

    def validate_version(self, value: str) -> str:
        return validate_node_major(value)

    def available_versions(self) -> List[str]:
        if not apt.apt_available():
            from app.config import get_settings

            if get_settings().dev_mode:
                return list(NODE_MAJORS)
            return []
        return [m for m in NODE_MAJORS if is_major_supported(m)]

    def installed_versions(self) -> List[str]:
        """The installed major, as a single-item list (or empty).

        Only the major is reported -- the granularity this provider actually
        manages, the same way PhpProvider reports "8.3" rather than a full
        dpkg patch version.
        """
        version = apt.installed_version(PACKAGE)
        if not version:
            return []
        major = version.split(".", 1)[0].lstrip("v")
        return [major] if major.isdigit() else []

    def is_version_installed(self, major: str) -> bool:
        major = validate_node_major(major)
        return major in self.installed_versions()

    # -- mutation ------------------------------------------------------

    def install(self, ctx, version: Optional[str] = None) -> None:
        if not version:
            raise ValidationError("A Node.js version is required.")
        major = validate_node_major(version)

        if self.is_version_installed(major):
            ctx.log(f"Node.js {major} is already installed")
            return

        available = self.available_versions()
        if major not in available:
            raise ValidationError(
                f"Node.js {major} is not available on this system. "
                f"Available: {', '.join(available) or 'none'}."
            )

        current = self.installed_versions()
        if current:
            ctx.log(f"Switching Node.js {current[0]} -> {major} (one version runs system-wide)")

        self._add_repository(ctx, major)
        apt.install(ctx, [PACKAGE])
        ctx.log(f"Node.js {major} installed (npm is bundled)")

    def uninstall(self, ctx, version: Optional[str] = None) -> None:
        ctx.log("Removing Node.js")
        apt.remove(ctx, [PACKAGE], purge=True)
        if SOURCE_LIST.exists():
            SOURCE_LIST.unlink()
            ctx.log(f"removed {SOURCE_LIST}")
        ctx.log("Node.js removed")

    def _add_repository(self, ctx, major: str) -> None:
        """Point apt at the NodeSource repo for ``major`` and refresh it.

        Downloads the setup script to a securely-created temp file rather
        than piping ``curl | bash`` -- this codebase never passes a string
        to a shell, only argument lists (see app.shell) -- then runs the
        downloaded file directly.
        """
        ctx.log(f"Adding the NodeSource repository for Node.js {major}.x")
        with tempfile.TemporaryDirectory() as tmp:
            script = str(Path(tmp) / "nodesource-setup.sh")
            ctx.check(
                ["curl", "-fsSL", "-o", script, f"https://deb.nodesource.com/setup_{major}.x"],
                timeout=60,
            )
            ctx.check(["bash", script], timeout=180)
