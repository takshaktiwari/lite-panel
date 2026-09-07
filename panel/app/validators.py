"""Input validation -- the choke point every untrusted value passes through.

Nothing from a browser reaches a subprocess argument, a SQL identifier, a
config filename, or a filesystem path without first being validated here.  The
daemon runs as root, so each function is written to *allowlist* a narrow shape
rather than to strip or escape a hostile one: a value that isn't obviously
fine is rejected, not repaired.

Every function raises :class:`ValidationError` on bad input and returns the
normalised value on success.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Union


class ValidationError(ValueError):
    """Raised when untrusted input fails a check.  Safe to show to the user."""


# --------------------------------------------------------------------------
# Domains
# --------------------------------------------------------------------------

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_DOMAIN_LEN = 253


def validate_domain(value: str) -> str:
    """A hostname safe to use as a filename, an nginx ``server_name``, and a
    ``certbot -d`` argument.

    Deliberately stricter than the DNS spec: no trailing dot, no underscores,
    no leading hyphen, and no all-numeric TLD (which would let an IP address
    through and confuse both nginx and certbot).
    """
    value = _clean(value, "domain").lower().rstrip(".")

    if not value:
        raise ValidationError("Domain is required.")
    if len(value) > _MAX_DOMAIN_LEN:
        raise ValidationError(f"Domain must be at most {_MAX_DOMAIN_LEN} characters.")
    if "/" in value or "\\" in value or ".." in value:
        raise ValidationError("Domain contains invalid characters.")

    labels = value.split(".")
    if len(labels) < 2:
        raise ValidationError("Enter a full domain, for example example.com.")
    for label in labels:
        if not _LABEL.match(label):
            raise ValidationError(
                f"'{label}' is not a valid domain part. Use letters, digits and "
                "hyphens, not starting or ending with a hyphen."
            )
    if labels[-1].isdigit():
        raise ValidationError("Domain must end in a name, not a number.")

    return value


# --------------------------------------------------------------------------
# Sites and system users
# --------------------------------------------------------------------------

_SITE_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
_USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_ADMIN_USERNAME = re.compile(r"^[a-z0-9]([a-z0-9._-]{0,30}[a-z0-9])?$")

# Accounts that already exist on a Debian/Ubuntu box, plus the ones we create
# ourselves.  Handing a site one of these would hand it that account's files.
RESERVED_USERNAMES = frozenset(
    {
        "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail",
        "news", "uucp", "proxy", "www-data", "backup", "list", "irc", "gnats",
        "nobody", "systemd-network", "systemd-resolve", "messagebus", "syslog",
        "_apt", "tss", "uuidd", "tcpdump", "landscape", "pollinate", "sshd",
        "mysql", "postgres", "redis", "ftp", "ubuntu", "admin", "adm",
        "lite-panel", "litepanel", "panel",
    }
)


def validate_site_name(value: str) -> str:
    """A short slug identifying a site; becomes part of its system username
    and of every config filename the panel owns for it."""
    value = _clean(value, "site name").lower()
    if not value:
        raise ValidationError("Site name is required.")
    if not _SITE_NAME.match(value):
        raise ValidationError(
            "Site name must be 1-32 characters: lowercase letters, digits and "
            "hyphens, not starting or ending with a hyphen."
        )
    return value


def validate_admin_username(value: str) -> str:
    """A login name for the panel itself.

    Deliberately *not* :func:`validate_system_username`: a panel admin is an
    application account, not a Linux one, so it never becomes an argument to
    ``useradd`` and the system reserved-name list does not apply -- which is
    why the obvious choice, ``admin``, is allowed here and refused there.
    """
    value = _clean(value, "username").lower()
    if not value:
        raise ValidationError("Username is required.")
    if not _ADMIN_USERNAME.match(value):
        raise ValidationError(
            "Username must be 1-32 characters using lowercase letters, digits, "
            "dots, hyphens and underscores, starting and ending with a letter "
            "or digit."
        )
    return value


def validate_system_username(value: str) -> str:
    """A Linux account name we are willing to create or act on."""
    value = _clean(value, "username").lower()
    if not value:
        raise ValidationError("Username is required.")
    if not _USERNAME.match(value):
        raise ValidationError(
            "Username must be 1-32 characters, start with a letter or "
            "underscore, and contain only lowercase letters, digits, hyphens "
            "and underscores."
        )
    if value in RESERVED_USERNAMES:
        raise ValidationError(f"'{value}' is a reserved system account.")
    return value


def site_username(site_name: str) -> str:
    """Derive a site's system user from its name.

    The prefix keeps site accounts in their own namespace, so a site can never
    collide with a pre-existing system account no matter what it's called.
    """
    return validate_system_username(f"site_{validate_site_name(site_name)}".replace("-", "_"))


# --------------------------------------------------------------------------
# Database identifiers
# --------------------------------------------------------------------------

_DB_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

RESERVED_SCHEMAS = frozenset({"mysql", "information_schema", "performance_schema", "sys"})


def validate_db_identifier(value: str, *, kind: str = "name") -> str:
    """A MySQL/MariaDB database or user name.

    MariaDB permits far more than this inside backticks; we allow only the
    boring subset so that quoting can never be the thing standing between us
    and injection.
    """
    value = _clean(value, f"database {kind}")
    if not value:
        raise ValidationError(f"Database {kind} is required.")
    if not _DB_IDENT.match(value):
        raise ValidationError(
            f"Database {kind} must be 1-64 characters, start with a letter or "
            "underscore, and contain only letters, digits and underscores."
        )
    if value.lower() in RESERVED_SCHEMAS:
        raise ValidationError(f"'{value}' is a reserved MariaDB schema.")
    return value


def quote_identifier(value: str) -> str:
    """Backtick-quote an identifier that has already been validated.

    The regex above forbids backticks, so this cannot be used to break out;
    the assertion is a tripwire in case someone calls it on a raw value.
    """
    if "`" in value or "\x00" in value:
        raise ValidationError("Invalid identifier.")
    return f"`{value}`"


# --------------------------------------------------------------------------
# Packages and versions
# --------------------------------------------------------------------------

_PHP_VERSION = re.compile(r"^[5-9]\.[0-9]$")
_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,99}$")


def validate_php_version(value: str) -> str:
    value = _clean(value, "PHP version")
    if not _PHP_VERSION.match(value):
        raise ValidationError("PHP version must look like 8.3.")
    return value


def validate_package_name(value: str) -> str:
    """An apt package name.

    Extension checkboxes in the UI turn into ``apt install`` arguments, so this
    guards against a submitted value that looks like a flag (``--force-yes``)
    or a path.
    """
    value = _clean(value, "package name").lower()
    if not _PACKAGE.match(value):
        raise ValidationError(f"'{value}' is not a valid package name.")
    return value


# --------------------------------------------------------------------------
# Filesystem containment
# --------------------------------------------------------------------------


def resolve_within(root: Union[str, Path], candidate: Union[str, Path]) -> Path:
    """Resolve ``candidate`` and prove it stays inside ``root``.

    This is the control the file manager rests on.  ``realpath`` is what makes
    it hold: it collapses ``..`` *and* follows symlinks, so neither a crafted
    relative path nor a symlink planted inside a site's own directory can
    reach outside the tree.  The candidate need not exist yet -- resolution of
    a not-yet-created file resolves its existing parents, which is what we
    want for uploads and new directories.
    """
    root_real = Path(os.path.realpath(str(root)))
    raw = str(candidate)

    if "\x00" in raw:
        raise ValidationError("Path contains a null byte.")

    target = Path(raw)
    if not target.is_absolute():
        target = root_real / target

    resolved = Path(os.path.realpath(str(target)))

    try:
        common = os.path.commonpath([str(root_real), str(resolved)])
    except ValueError:  # different drives / unrelated roots
        raise ValidationError("Path is outside the permitted directory.") from None

    if common != str(root_real):
        raise ValidationError("Path is outside the permitted directory.")

    return resolved


def validate_filename(value: str) -> str:
    """A single path component -- no directory traversal, no separators."""
    value = _clean(value, "file name")
    if not value or value in {".", ".."}:
        raise ValidationError("Invalid file name.")
    if "/" in value or "\\" in value:
        raise ValidationError("File name cannot contain a path separator.")
    if len(value) > 255:
        raise ValidationError("File name is too long.")
    return value


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _clean(value, field: str) -> str:
    """Coerce to a stripped string, rejecting the shapes that break parsers."""
    if value is None:
        raise ValidationError(f"{field.capitalize()} is required.")
    if not isinstance(value, str):
        raise ValidationError(f"{field.capitalize()} must be text.")
    if "\x00" in value:
        raise ValidationError(f"{field.capitalize()} contains a null byte.")
    value = value.strip()
    if any(ch in value for ch in "\r\n\t"):
        raise ValidationError(f"{field.capitalize()} cannot contain line breaks.")
    return value
