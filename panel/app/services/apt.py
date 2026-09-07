"""Talking to apt and dpkg.

Package *discovery* lives here so that no version list is ever hardcoded: the
panel asks the machine what it can install, which is what lets it offer
whatever PHP versions exist at the time rather than whatever was current when
this was written.

The parsing functions are deliberately pure and take the command output as a
string, so they can be tested on a workstation with no apt present.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Sequence

from app.shell import CommandError, run, which
from app.validators import validate_package_name

logger = logging.getLogger(__name__)

# ondrej/php is the de facto source of multiple PHP versions on Ubuntu; Debian
# uses Sury, the same maintainer. Without one of these a box offers only the
# single PHP version its release shipped with.
PHP_PPA_UBUNTU = "ppa:ondrej/php"
SURY_REPO_DEBIAN = "https://packages.sury.org/php/"


def is_php_ppa_supported(os_id: str) -> bool:
    """Whether ondrej/php PPA has a release for this distribution's codename.

    Deliberately uncached: this is a live probe of a third party's mirror, and
    a brand-new release (like the "resolute" box this was caught on) can gain
    support later. A cached wrong answer for the life of the process is worse
    than the cost of re-checking the handful of times this is actually called
    -- once per PHP repository setup attempt, not once per page load.
    """
    if os_id == "debian":
        return True
    if os_id != "ubuntu":
        return False
    try:
        codename = run(["lsb_release", "-sc"], check=False, timeout=10).stdout.strip()
        if not codename:
            return False
        test_url = f"https://ppa.launchpadcontent.net/ondrej/php/ubuntu/dists/{codename}/Release"
        result = run(["curl", "-fsSL", "--head", "--max-time", "5", test_url], check=False, timeout=10)
        return result.returncode == 0
    except (CommandError, OSError):
        return False


def apt_available() -> bool:
    return which("apt-get") is not None


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------


def package_names(prefix: str) -> List[str]:
    """Every installable package name starting with ``prefix``.

    ``apt-cache pkgnames`` is used rather than ``search`` because it matches on
    the name only and does not depend on package descriptions being indexed.
    """
    if not apt_available():
        return []
    try:
        result = run(["apt-cache", "pkgnames", prefix], check=False, timeout=60)
    except (CommandError, OSError) as exc:
        logger.warning("apt-cache pkgnames failed: %s", exc)
        return []
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def parse_apt_cache_policy_has_candidate(policy_output: str) -> bool:
    """Whether ``apt-cache policy`` output shows a real install candidate.

    Pure parser, tested against captured output. A package name can appear in
    ``apt-cache pkgnames`` results, or be referenced in another package's
    dependency metadata, without there being anything real to install -- that
    mismatch is exactly what produced "Package X is not available, but is
    referred to by another package" on a release the PHP PPA doesn't yet
    cover. Absence of any "Candidate:" line at all (an unknown package name)
    also means no candidate.
    """
    for line in policy_output.splitlines():
        line = line.strip()
        if line.startswith("Candidate:"):
            return "(none)" not in line
    return False


def has_candidate(package: str) -> bool:
    """Whether apt can actually install this package right now.

    This is the check that stands between offering a version in the UI and it
    actually being installable -- see parse_apt_cache_policy_has_candidate.
    """
    package = validate_package_name(package)
    if not apt_available():
        return False
    try:
        result = run(["apt-cache", "policy", package], check=False, timeout=30)
    except (CommandError, OSError):
        return False
    return parse_apt_cache_policy_has_candidate(result.stdout)


def is_installed(package: str) -> bool:
    """Whether dpkg considers a package installed."""
    package = validate_package_name(package)
    if not which("dpkg-query"):
        return False
    try:
        result = run(
            ["dpkg-query", "-W", "-f=${Status}", package],
            check=False,
            timeout=30,
        )
    except (CommandError, OSError):
        return False
    return "install ok installed" in result.stdout


def installed_version(package: str) -> Optional[str]:
    package = validate_package_name(package)
    if not which("dpkg-query"):
        return None
    try:
        result = run(
            ["dpkg-query", "-W", "-f=${Status}|${Version}", package],
            check=False,
            timeout=30,
        )
    except (CommandError, OSError):
        return None
    if "install ok installed" not in result.stdout:
        return None
    _, _, version = result.stdout.partition("|")
    return version.strip() or None


def installed_packages(prefix: str) -> Dict[str, str]:
    """Installed packages matching a prefix, mapped to their versions."""
    if not which("dpkg-query"):
        return {}
    try:
        result = run(
            ["dpkg-query", "-W", "-f=${Package}|${Status}|${Version}\n", f"{prefix}*"],
            check=False,
            timeout=30,
        )
    except (CommandError, OSError):
        return {}
    return parse_dpkg_query(result.stdout)


def parse_dpkg_query(output: str) -> Dict[str, str]:
    """Parse ``Package|Status|Version`` lines into installed packages only."""
    found: Dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        package, status, version = (p.strip() for p in parts)
        if status == "install ok installed" and package:
            found[package] = version
    return found


# --------------------------------------------------------------------------
# Mutations (all run inside a job, so they stream their output)
# --------------------------------------------------------------------------


def update(ctx) -> None:
    ctx.check(["apt-get", "update", "-y"], timeout=600)


def install(ctx, packages: Sequence[str], *, timeout: int = 1800) -> None:
    """Install packages non-interactively.

    Every name is validated first: these come from checkboxes in the UI, and a
    value shaped like a flag would otherwise become one.
    """
    validated = [validate_package_name(p) for p in packages]
    if not validated:
        return
    ctx.check(
        [
            "apt-get", "install", "-y",
            "--no-install-recommends",
            # Keep the operator's existing config files on any upgrade; the
            # panel owns its own drop-ins and never wants a prompt here.
            "-o", "Dpkg::Options::=--force-confdef",
            "-o", "Dpkg::Options::=--force-confold",
            *validated,
        ],
        timeout=timeout,
    )


def remove(ctx, packages: Sequence[str], *, purge: bool = False, timeout: int = 900) -> None:
    validated = [validate_package_name(p) for p in packages]
    if not validated:
        return
    ctx.check(
        ["apt-get", "purge" if purge else "remove", "-y", "--auto-remove", *validated],
        timeout=timeout,
    )


def ensure_php_repository(ctx, os_id: str) -> None:
    """Add the third-party PHP repository if it isn't already present.

    This is what makes multiple PHP versions available at all. Ubuntu gets the
    PPA; Debian gets Sury directly, since ``add-apt-repository`` has no PPA
    support there.

    On Ubuntu releases not yet supported by ondrej/php (e.g. a brand-new LTS
    that the PPA maintainer hasn't caught up with) we skip the PPA silently:
    the OS ships at least one PHP version itself, which is still installable.
    """
    if os_id == "ubuntu":
        if _ppa_present("ondrej"):
            ctx.log("ondrej/php repository already configured")
            return

        if not is_php_ppa_supported(os_id):
            codename = ""
            try:
                codename = run(["lsb_release", "-sc"], check=False, timeout=10).stdout.strip()
            except (CommandError, OSError):
                pass
            ctx.log(
                f"ondrej/php PPA does not yet support Ubuntu '{codename}'. "
                "Installing from the OS repository instead — only the version "
                "Ubuntu ships is available on this machine."
            )
            return

        install(ctx, ["software-properties-common", "ca-certificates"])
        ctx.check(["add-apt-repository", "-y", PHP_PPA_UBUNTU], timeout=300)
        update(ctx)
        return

    if os_id == "debian":
        if _sury_present():
            ctx.log("sury.org repository already configured")
            return
        install(ctx, ["apt-transport-https", "lsb-release", "ca-certificates", "curl", "gnupg"])
        ctx.check(
            [
                "curl", "-fsSL", "-o", "/usr/share/keyrings/deb.sury.org-php.gpg",
                "https://packages.sury.org/php/apt.gpg",
            ],
            timeout=120,
        )
        _write_sury_source(ctx)
        update(ctx)
        return

    raise RuntimeError(f"Unsupported distribution '{os_id}'; expected ubuntu or debian.")



def _write_sury_source(ctx) -> None:
    from pathlib import Path

    from app.shell import run as _run

    codename = _run(["lsb_release", "-sc"], check=False).stdout.strip() or "bookworm"
    line = (
        "deb [signed-by=/usr/share/keyrings/deb.sury.org-php.gpg] "
        f"{SURY_REPO_DEBIAN} {codename} main\n"
    )
    Path("/etc/apt/sources.list.d/php.list").write_text(line, encoding="utf-8")
    ctx.log(f"wrote /etc/apt/sources.list.d/php.list for {codename}")


def _ppa_present(needle: str) -> bool:
    from pathlib import Path

    for directory in (Path("/etc/apt/sources.list.d"),):
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if needle in entry.name:
                return True
            try:
                if entry.is_file() and needle in entry.read_text(encoding="utf-8", errors="ignore"):
                    return True
            except OSError:
                continue
    return False


def _sury_present() -> bool:
    return _ppa_present("sury") or _ppa_present("php.list")


# --------------------------------------------------------------------------
# PHP-specific discovery
# --------------------------------------------------------------------------

_PHP_FPM_PACKAGE = re.compile(r"^php(\d+\.\d+)-fpm$")
_PHP_EXTENSION = re.compile(r"^php(\d+\.\d+)-([a-z0-9+.-]+)$")

# Packages that are part of the panel's own baseline for a PHP version rather
# than optional extras the operator picks.
PHP_CORE_SUFFIXES = frozenset({"fpm", "cli", "common", "readline", "opcache"})


def parse_php_versions(pkgnames_output: str) -> List[str]:
    """Extract PHP versions from ``apt-cache pkgnames php`` output.

    A version counts as available only if its ``-fpm`` package exists, since
    the panel serves sites through FPM pools and a version without one is
    useless to it.
    """
    versions = set()
    for line in pkgnames_output.splitlines():
        match = _PHP_FPM_PACKAGE.match(line.strip())
        if match:
            versions.add(match.group(1))
    return sorted(versions, key=_version_key)


def parse_php_extensions(pkgnames_output: str, version: str) -> List[str]:
    """Extension names available for one PHP version, core packages excluded."""
    prefix = f"php{version}-"
    extensions = set()
    for line in pkgnames_output.splitlines():
        name = line.strip()
        if not name.startswith(prefix):
            continue
        match = _PHP_EXTENSION.match(name)
        if not match or match.group(1) != version:
            continue
        suffix = match.group(2)
        if suffix in PHP_CORE_SUFFIXES:
            continue
        extensions.add(suffix)
    return sorted(extensions)


def _version_key(version: str):
    try:
        major, minor = version.split(".", 1)
        return (int(major), int(minor))
    except ValueError:
        return (0, 0)


def sort_versions(versions: Sequence[str]) -> List[str]:
    return sorted(versions, key=_version_key)
