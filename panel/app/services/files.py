"""The file manager's filesystem operations.

Every path passes through :func:`app.validators.resolve_within` against the
sites root before anything touches the disk.  That check is the single most
load-bearing control in the panel: the daemon runs as root, so without it a
crafted path would read or overwrite any file on the machine.

``resolve_within`` uses ``realpath``, so it also defeats a symlink planted
inside a site's own directory pointing outward -- an attack a naive prefix
comparison misses.
"""

from __future__ import annotations

import logging
import os
import pwd
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from app.config import get_settings
from app.validators import ValidationError, resolve_within, validate_filename

logger = logging.getLogger(__name__)

settings = get_settings()

# Editing is for configuration and source files. Anything larger is either
# generated or binary, and loading it into a textarea helps nobody.
MAX_EDIT_BYTES = 1024 * 1024
MAX_UPLOAD_BYTES = 256 * 1024 * 1024

TEXT_EXTENSIONS = frozenset(
    {
        ".txt", ".md", ".html", ".htm", ".css", ".js", ".json", ".xml", ".yml",
        ".yaml", ".ini", ".conf", ".cfg", ".env", ".php", ".py", ".rb", ".sh",
        ".sql", ".log", ".csv", ".ts", ".jsx", ".tsx", ".vue", ".toml", ".lock",
        ".gitignore", ".htaccess",
    }
)


def root() -> Path:
    return Path(os.path.realpath(str(settings.sites_root)))


@dataclass
class Entry:
    name: str
    path: str
    relative: str
    is_dir: bool
    size: int
    modified: Optional[datetime]
    owner: str
    mode: str
    is_symlink: bool

    @property
    def size_display(self) -> str:
        return format_size(self.size) if not self.is_dir else "—"

    @property
    def editable(self) -> bool:
        if self.is_dir or self.size > MAX_EDIT_BYTES:
            return False
        return Path(self.name).suffix.lower() in TEXT_EXTENSIONS or "." not in self.name


def resolve(candidate) -> Path:
    """Resolve a user-supplied path, refusing anything outside the sites root."""
    return resolve_within(root(), candidate or ".")


def relative_to_root(path: Path) -> str:
    try:
        return str(Path(path).relative_to(root()))
    except ValueError:
        return "."


def list_directory(candidate) -> List[Entry]:
    target = resolve(candidate)
    if not target.is_dir():
        raise ValidationError("That path is not a directory.")

    entries: List[Entry] = []
    try:
        children = sorted(
            target.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
    except PermissionError:
        raise ValidationError("Permission denied reading that directory.") from None

    for child in children:
        entries.append(_describe(child))
    return entries


def _describe(path: Path) -> Entry:
    try:
        info = path.lstat()
        is_link = stat.S_ISLNK(info.st_mode)
        if is_link:
            info = path.stat()
    except OSError:
        info = None
        is_link = False

    try:
        owner = pwd.getpwuid(info.st_uid).pw_name if info else "?"
    except (KeyError, AttributeError):
        owner = str(info.st_uid) if info else "?"

    return Entry(
        name=path.name,
        path=str(path),
        relative=relative_to_root(path),
        is_dir=path.is_dir(),
        size=info.st_size if info else 0,
        modified=(
            datetime.fromtimestamp(info.st_mtime, tz=timezone.utc) if info else None
        ),
        owner=owner,
        mode=stat.filemode(info.st_mode) if info else "?",
        is_symlink=is_link,
    )


def breadcrumbs(candidate) -> List[dict]:
    """Path segments from the root down to the current directory."""
    target = resolve(candidate)
    crumbs = [{"name": "Home", "path": "."}]

    relative = relative_to_root(target)
    if relative in (".", ""):
        return crumbs

    accumulated = Path()
    for part in Path(relative).parts:
        accumulated = accumulated / part
        crumbs.append({"name": part, "path": str(accumulated)})
    return crumbs


def parent_of(candidate) -> str:
    target = resolve(candidate)
    if target == root():
        return "."
    return relative_to_root(target.parent)


# --------------------------------------------------------------------------
# Reads and writes
# --------------------------------------------------------------------------


def read_text(candidate) -> str:
    target = resolve(candidate)
    if not target.is_file():
        raise ValidationError("That file does not exist.")
    if target.stat().st_size > MAX_EDIT_BYTES:
        raise ValidationError(
            f"File is larger than {format_size(MAX_EDIT_BYTES)} and cannot be edited here."
        )
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValidationError("This looks like a binary file, so it cannot be edited.") from None


def write_text(candidate, content: str) -> None:
    target = resolve(candidate)
    if target.is_dir():
        raise ValidationError("That path is a directory.")

    owner = _owner_of(target if target.exists() else target.parent)
    target.write_text(content, encoding="utf-8")
    _restore_owner(target, owner)


def create_directory(parent_candidate, name: str) -> Path:
    name = validate_filename(name)
    parent = resolve(parent_candidate)
    target = resolve(parent / name)

    if target.exists():
        raise ValidationError(f"'{name}' already exists.")

    target.mkdir(parents=False)
    _restore_owner(target, _owner_of(parent))
    return target


def create_file(parent_candidate, name: str) -> Path:
    name = validate_filename(name)
    parent = resolve(parent_candidate)
    target = resolve(parent / name)

    if target.exists():
        raise ValidationError(f"'{name}' already exists.")

    target.touch()
    _restore_owner(target, _owner_of(parent))
    return target


def rename(candidate, new_name: str) -> Path:
    new_name = validate_filename(new_name)
    target = resolve(candidate)
    if target == root():
        raise ValidationError("The root directory cannot be renamed.")

    destination = resolve(target.parent / new_name)
    if destination.exists():
        raise ValidationError(f"'{new_name}' already exists.")

    target.rename(destination)
    return destination


def delete(candidate) -> str:
    target = resolve(candidate)
    if target == root():
        raise ValidationError("The root directory cannot be deleted.")

    name = target.name
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()
    return name


def save_upload(parent_candidate, filename: str, data: bytes) -> Path:
    filename = validate_filename(filename)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError(f"Uploads are limited to {format_size(MAX_UPLOAD_BYTES)}.")

    parent = resolve(parent_candidate)
    if not parent.is_dir():
        raise ValidationError("Upload target is not a directory.")

    target = resolve(parent / filename)
    target.write_bytes(data)
    # Files uploaded through the panel are written by root; without this the
    # site's own user could not modify what it just received.
    _restore_owner(target, _owner_of(parent))
    return target


# --------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------


def _owner_of(path: Path):
    try:
        info = path.stat()
        return (info.st_uid, info.st_gid)
    except OSError:
        return None


def _restore_owner(path: Path, owner) -> None:
    if not owner:
        return
    try:
        os.chown(path, owner[0], owner[1])
    except (OSError, AttributeError) as exc:
        logger.debug("could not set ownership on %s: %s", path, exc)


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
