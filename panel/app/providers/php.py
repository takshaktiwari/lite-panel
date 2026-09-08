"""PHP — the provider that proves the model.

Several versions are installed side by side and each site picks one, so
nothing here is allowed to assume a single version.  The versions on offer and
the extensions available for each are discovered from apt at runtime; there is
no hardcoded list of either.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from app.providers.base import Provider, register
from app.services import apt, system
from app.validators import ValidationError, validate_package_name, validate_php_version

logger = logging.getLogger(__name__)

FPM_SOCKET_DIR = Path("/run/php")

# Installed alongside every version so a site works out of the box.  This is a
# starting point, not a fixed list: each one can be removed and any other
# extension added afterwards from the Stack page.
DEFAULT_EXTENSIONS = (
    "mysql", "mbstring", "xml", "curl", "zip", "gd", "intl", "bcmath", "soap",
)

# Always installed with a version; not offered as removable extensions.
CORE_PACKAGES = ("fpm", "cli", "common")

# Used only when apt itself is unavailable (macOS development) so the UI has
# something to render. Never trusted as evidence that a version can actually
# be installed — see available_versions(). A prior version of this file used
# a similar list as a *production* fallback merged into the real result,
# which is what let PHP 8.2 be offered (and then fail) on a machine where it
# does not exist: the ondrej/php PPA has no build yet for this Ubuntu release,
# and OS repos on it ship only 8.5. Never reintroduce that merge.
_DEV_FIXTURE_VERSIONS = ["8.1", "8.2", "8.3"]


@register
class PhpProvider(Provider):
    key = "php"
    name = "PHP"
    description = "PHP-FPM. Install several versions and assign one per site."
    multi_version = True
    service_name = None  # one unit per version; see service_for()

    # -- discovery ---------------------------------------------------------

    def validate_version(self, value: str) -> str:
        return validate_php_version(value)

    def available_versions(self) -> List[str]:
        """PHP versions genuinely installable on this machine right now.

        Only ever answers from live apt data -- never from a static list of
        versions the PPA carries *in general*, because "the PPA generally
        carries 8.2" and "8.2 is installable on this specific release right
        now" are different facts, and only the second one is safe to act on.
        A brand-new Ubuntu release with no PPA build yet, or OS repos that
        ship only one version, are exactly the cases where they diverge.

        Each candidate's ``-fpm`` package is verified with
        ``apt.has_candidate`` rather than trusting ``apt-cache pkgnames``
        alone: a name can appear there (or be referenced by another
        package's dependencies) without anything real to install.
        """
        if not apt.apt_available():
            # No apt on this machine at all (e.g. local development on
            # macOS) -- fixture data so the UI has something to render.
            from app.config import get_settings

            if get_settings().dev_mode:
                return apt.sort_versions(list(_DEV_FIXTURE_VERSIONS))
            return []

        candidates = apt.parse_php_versions("\n".join(apt.package_names("php")))
        verified = [v for v in candidates if apt.has_candidate(f"php{v}-fpm")]
        return apt.sort_versions(verified)


    def repository_status(self) -> Dict:
        """Whether ondrej/php (or Sury) is configured, and whether it could be.

        Drives the explicit "add repository" affordance in the UI: the only
        safe way to widen available_versions() beyond whatever the OS shipped
        is to actually add the repository and let apt tell us what's really
        there afterward -- never to guess.
        """
        os_id = system.os_release_id()
        present = apt._ppa_present("ondrej") or apt._sury_present()
        return {
            "present": present,
            "supported": True if present else apt.is_php_ppa_supported(os_id),
            "os_id": os_id,
        }

    def add_repository(self, ctx) -> None:
        """Add the PHP repository as its own action, independent of any
        specific version install."""
        os_id = system.os_release_id()
        if not os_id:
            raise ValidationError("Could not detect the OS distribution.")
        apt.ensure_php_repository(ctx, os_id)
        newly_available = self.available_versions()
        ctx.log(f"Versions now available: {', '.join(newly_available) or 'none'}")

    def installed_versions(self) -> List[str]:
        installed = apt.installed_packages("php")
        versions = []
        for package in installed:
            if package.endswith("-fpm") and package.startswith("php"):
                version = package[3:-4]
                if version and version[0].isdigit():
                    versions.append(version)
        return apt.sort_versions(versions)

    def is_version_installed(self, version: str) -> bool:
        version = validate_php_version(version)
        return apt.is_installed(f"php{version}-fpm")

    def service_for(self, version: str) -> str:
        return f"php{validate_php_version(version)}-fpm"

    def socket_for(self, version: str) -> str:
        return str(FPM_SOCKET_DIR / f"php{validate_php_version(version)}-fpm.sock")

    def cli_path_for(self, version: str) -> str:
        """The CLI binary for a version -- what a cron job or shell script
        should actually invoke. Installed by the "cli" package in
        CORE_PACKAGES alongside fpm, so it exists for every version this
        provider manages."""
        return f"/usr/bin/php{validate_php_version(version)}"

    def detect_socket(self) -> Optional[str]:
        """Any live FPM socket, used when a site has no explicit version."""
        if not FPM_SOCKET_DIR.is_dir():
            return None
        sockets = sorted(FPM_SOCKET_DIR.glob("php*-fpm.sock"))
        return str(sockets[-1]) if sockets else None

    # -- extensions --------------------------------------------------------

    def available_extensions(self, version: str) -> List[str]:
        version = validate_php_version(version)
        return apt.parse_php_extensions("\n".join(apt.package_names(f"php{version}-")), version)

    def installed_extensions(self, version: str) -> List[str]:
        version = validate_php_version(version)
        prefix = f"php{version}-"
        found = []
        for package in apt.installed_packages(prefix):
            if not package.startswith(prefix):
                continue
            suffix = package[len(prefix):]
            if suffix and suffix not in apt.PHP_CORE_SUFFIXES:
                found.append(suffix)
        return sorted(found)

    def extension_report(self, version: str) -> List[Dict]:
        """Every available extension with whether it is installed.

        This is what the Stack page renders as checkboxes -- computed from the
        machine, so it is correct for whatever version is being looked at.
        """
        installed = set(self.installed_extensions(version))
        return [
            {"name": name, "installed": name in installed}
            for name in self.available_extensions(version)
        ]

    def install_extensions(self, ctx, version: str, extensions) -> None:
        version = validate_php_version(version)
        packages = [f"php{version}-{validate_package_name(e)}" for e in extensions]
        if not packages:
            ctx.log("no extensions selected")
            return
        apt.install(ctx, packages)
        self._restart(ctx, version)

    def remove_extensions(self, ctx, version: str, extensions) -> None:
        version = validate_php_version(version)
        names = [validate_package_name(e) for e in extensions]
        for name in names:
            if name in apt.PHP_CORE_SUFFIXES:
                raise ValidationError(f"'{name}' is part of PHP itself and cannot be removed.")
        packages = [f"php{version}-{name}" for name in names]
        if not packages:
            return
        apt.remove(ctx, packages)
        self._restart(ctx, version)

    # -- mutation ----------------------------------------------------------

    def install(self, ctx, version: Optional[str] = None) -> None:
        if not version:
            raise ValidationError("A PHP version is required.")
        version = validate_php_version(version)

        if self.is_version_installed(version):
            ctx.log(f"PHP {version} is already installed")
            return

        ctx.log(f"Installing PHP {version}")
        apt.ensure_php_repository(ctx, system.os_release_id())

        available = self.available_versions()
        if version not in available:
            raise ValidationError(
                f"PHP {version} is not available on this system. "
                f"Available: {', '.join(available) or 'none'}."
            )

        packages = [f"php{version}-{suffix}" for suffix in CORE_PACKAGES]
        packages += [f"php{version}-{ext}" for ext in DEFAULT_EXTENSIONS]
        apt.install(ctx, packages)

        ctx.check(["systemctl", "enable", "--now", self.service_for(version)], timeout=120)
        self.render_config(ctx, version=version)
        ctx.log(f"PHP {version} installed")

    def uninstall(self, ctx, version: Optional[str] = None) -> None:
        if not version:
            raise ValidationError("A PHP version is required.")
        version = validate_php_version(version)

        # Refuse while a site still points at it: removing the version would
        # leave that site's vhost pointing at a socket that no longer exists.
        in_use = self._sites_using(version)
        if in_use:
            raise ValidationError(
                f"PHP {version} is still used by: {', '.join(in_use)}. "
                "Move those sites to another version first."
            )

        ctx.log(f"Removing PHP {version}")
        ctx.run(["systemctl", "disable", "--now", self.service_for(version)])

        packages = sorted(apt.installed_packages(f"php{version}-"))
        if packages:
            apt.remove(ctx, packages, purge=True)
        ctx.log(f"PHP {version} removed")

    def render_config(self, ctx=None, version: Optional[str] = None) -> None:
        """Write the panel's php.ini drop-in for one or every version."""
        from app.services import renderer, tuning

        versions = [version] if version else self.installed_versions()
        for item in versions:
            item = validate_php_version(item)
            target = Path(f"/etc/php/{item}/fpm/conf.d/99-lite-panel.ini")
            if not target.parent.is_dir():
                if ctx:
                    ctx.log(f"skipping {item}: {target.parent} does not exist")
                continue
            renderer.render_to_file(
                "php-ini.conf.j2",
                target,
                {"settings": tuning.php_settings(), "version": item},
            )
            if ctx:
                ctx.log(f"wrote {target}")
                self._restart(ctx, item)

    def _restart(self, ctx, version: str) -> None:
        ctx.run(["systemctl", "restart", self.service_for(version)])

    def _sites_using(self, version: str) -> List[str]:
        from sqlalchemy import select

        from app.database import session_scope
        from app.models import Site

        with session_scope() as db:
            return [
                site.domain
                for site in db.scalars(select(Site).where(Site.php_version == version)).all()
            ]

    def status(self):
        state = super().status()
        # Report the per-version FPM units rather than a single service.
        active = [v for v in state.versions if system.service_state(self.service_for(v)).active]
        state.service_installed = bool(state.versions)
        state.service_active = bool(active)
        state.detail = f"{len(active)}/{len(state.versions)} running" if state.versions else ""
        return state
