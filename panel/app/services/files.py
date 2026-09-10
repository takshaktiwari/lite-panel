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
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from app.config import get_settings
from app.validators import (
    ValidationError,
    resolve_within,
    validate_chmod_scope,
    validate_filename,
    validate_permission_mode,
)

logger = logging.getLogger(__name__)

settings = get_settings()

# Editing is for configuration and source files. Anything larger is either
# generated or binary, and loading it into a textarea helps nobody.
MAX_EDIT_BYTES = 1024 * 1024
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB

# Guardrails for archive creation/extraction. These bound how much work one
# panel click can trigger -- not a security control by themselves, but the
# zip-slip check below is, and it is not optional.
MAX_ARCHIVE_INPUT_BYTES = 1024 * 1024 * 1024  # 1 GB of source data per archive
# A fixed uncompressed-size cap has no good value -- one server has 20GB
# free, another has 2TB. Instead extraction is only refused when it would
# leave less than this much space on the actual destination filesystem, so
# the limit is always the box's own disk rather than a number that goes
# stale the moment someone provisions a bigger one.
EXTRACT_FREE_SPACE_MARGIN_BYTES = 512 * 1024 * 1024  # 512 MB

# Chunked uploads write straight into their destination directory as
# ".lpu-<id>.part" so the final rename is always same-filesystem and instant
# regardless of file size. Sessions live in a process-local dict -- the panel
# runs a single Uvicorn worker, and an upload is not meant to survive a
# refresh or a restart, only the browser tab that started it.
UPLOAD_FREE_SPACE_MARGIN_BYTES = 512 * 1024 * 1024  # 512 MB
CHUNK_UPLOAD_PREFIX = ".lpu-"
CHUNK_UPLOAD_SUFFIX = ".part"
UPLOAD_SESSION_TTL = timedelta(hours=2)

ARCHIVE_EXTENSIONS = frozenset({".zip"})

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


def _suffix(name: str) -> str:
    """Like Path(name).suffix, but treats a bare dotfile's own dot as the
    extension too -- Path(".env").suffix is "" (pathlib reads the whole name
    as the stem), which would otherwise leave .env/.gitignore/.htaccess
    unmatched despite being listed in TEXT_EXTENSIONS."""
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[1]


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
    permissions: str
    is_symlink: bool

    @property
    def size_display(self) -> str:
        return format_size(self.size) if not self.is_dir else "—"

    @property
    def editable(self) -> bool:
        if self.is_dir or self.size > MAX_EDIT_BYTES:
            return False
        return _suffix(self.name).lower() in TEXT_EXTENSIONS or "." not in self.name

    @property
    def is_archive(self) -> bool:
        return not self.is_dir and Path(self.name).suffix.lower() in ARCHIVE_EXTENSIONS


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
        if _is_partial_upload_name(child.name):
            continue
        entries.append(_describe(child))
    return entries


def _is_partial_upload_name(name: str) -> bool:
    return name.startswith(CHUNK_UPLOAD_PREFIX) and name.endswith(CHUNK_UPLOAD_SUFFIX)


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
        permissions=(f"{stat.S_IMODE(info.st_mode):03o}" if info else "?"),
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


def _unique_name(parent: Path, original_name: str, *, tag: str = "copy") -> str:
    """A name like "config-copy.php" that doesn't collide in ``parent``,
    trying "-copy-2", "-copy-3", ... if it does."""
    stem, suffix = Path(original_name).stem, Path(original_name).suffix
    candidate = f"{stem}-{tag}{suffix}"
    counter = 2
    while (parent / candidate).exists():
        candidate = f"{stem}-{tag}-{counter}{suffix}"
        counter += 1
    return candidate


def _is_within(path: Path, ancestor: Path) -> bool:
    try:
        path.relative_to(ancestor)
        return True
    except ValueError:
        return False


def copy_item(
    candidate, *, destination_dir: Optional[str] = None, new_name: Optional[str] = None
) -> Path:
    """Copy a file or directory.

    With no destination, duplicates in place next to the original under an
    auto-generated name -- the "back this up before I touch it" case, which
    is exactly what an emergency edit calls for. With a destination, copies
    into that directory instead, keeping the original name unless
    ``new_name`` is given.
    """
    source = resolve(candidate)
    if source == root():
        raise ValidationError("The root directory cannot be copied.")
    if not source.exists():
        raise ValidationError("That path does not exist.")

    if destination_dir is not None:
        dest_parent = resolve(destination_dir)
        if not dest_parent.is_dir():
            raise ValidationError("Destination is not a directory.")
        name = validate_filename(new_name) if new_name else source.name
    else:
        dest_parent = source.parent
        name = validate_filename(new_name) if new_name else _unique_name(dest_parent, source.name)

    destination = resolve(dest_parent / name)
    if destination == source:
        raise ValidationError("Source and destination are the same.")
    if destination.exists():
        raise ValidationError(f"'{name}' already exists there.")
    if source.is_dir() and _is_within(destination, source):
        raise ValidationError("Cannot copy a folder into itself.")

    owner = _owner_of(dest_parent)
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=False)
    else:
        shutil.copy2(source, destination)
    _restore_owner_recursive(destination, owner)
    return destination


def move_item(candidate, destination_dir: str, *, new_name: Optional[str] = None) -> Path:
    """Move a file or directory into ``destination_dir``, keeping its name
    unless ``new_name`` is given.

    Unlike :func:`copy_item`, this always needs a destination -- moving
    something "in place" is just a rename, which already exists as
    :func:`rename`.
    """
    source = resolve(candidate)
    if source == root():
        raise ValidationError("The root directory cannot be moved.")
    if not source.exists():
        raise ValidationError("That path does not exist.")

    dest_parent = resolve(destination_dir)
    if not dest_parent.is_dir():
        raise ValidationError("Destination is not a directory.")
    name = validate_filename(new_name) if new_name else source.name

    destination = resolve(dest_parent / name)
    if destination == source:
        raise ValidationError("Source and destination are the same.")
    if destination.exists():
        raise ValidationError(f"'{name}' already exists there.")
    if source.is_dir() and _is_within(destination, source):
        raise ValidationError("Cannot move a folder into itself.")

    owner = _owner_of(source)
    shutil.move(str(source), str(destination))
    # Same-filesystem moves are a plain rename and keep ownership as-is;
    # this only matters if shutil.move had to fall back to copy+delete
    # (crossing a filesystem boundary), but it is harmless either way.
    _restore_owner_recursive(destination, owner)
    return destination


def chmod(candidate, mode: str) -> Path:
    """Set the permission bits of a single file or folder."""
    target = resolve(candidate)
    if target == root():
        raise ValidationError("The root directory's permissions cannot be changed.")
    if not target.exists():
        raise ValidationError("That path does not exist.")

    os.chmod(target, validate_permission_mode(mode))
    return target


def chmod_recursive(candidate, mode: str, scope: str) -> int:
    """Apply ``mode`` to every file, folder, or both, inside ``candidate``
    (the directory itself is left untouched -- this is for its contents).

    Walks with ``followlinks=False``: a symlink planted inside a site's own
    directory must never let a recursive chmod reach a file it doesn't
    actually own, the same containment concern every other tree-walking
    operation in this module guards against.
    """
    target = resolve(candidate)
    if not target.is_dir():
        raise ValidationError("That path is not a directory.")

    numeric_mode = validate_permission_mode(mode)
    scope = validate_chmod_scope(scope)

    count = 0
    for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
        current = Path(dirpath)
        names = []
        if scope in ("dirs", "both"):
            names.extend(dirnames)
        if scope in ("files", "both"):
            names.extend(filenames)
        for name in names:
            child = current / name
            if child.is_symlink():
                continue
            os.chmod(child, numeric_mode)
            count += 1
    return count


def bulk_chmod(candidates: List[str], mode: str) -> Tuple[List[str], List[Tuple[str, str]]]:
    succeeded: List[str] = []
    failed: List[Tuple[str, str]] = []
    for candidate in candidates:
        try:
            chmod(candidate, mode)
            succeeded.append(candidate)
        except (ValidationError, OSError) as exc:
            failed.append((candidate, str(exc)))
    return succeeded, failed


def bulk_move(candidates: List[str], destination_dir: str) -> Tuple[List[str], List[Tuple[str, str]]]:
    succeeded: List[str] = []
    failed: List[Tuple[str, str]] = []
    for candidate in candidates:
        try:
            move_item(candidate, destination_dir)
            succeeded.append(candidate)
        except (ValidationError, OSError) as exc:
            failed.append((candidate, str(exc)))
    return succeeded, failed


def bulk_delete(candidates: List[str]) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Delete several paths, continuing past individual failures.

    Returns (names deleted, [(candidate, error), ...]) so the caller can
    report a partial success honestly rather than all-or-nothing.
    """
    succeeded: List[str] = []
    failed: List[Tuple[str, str]] = []
    for candidate in candidates:
        try:
            succeeded.append(delete(candidate))
        except (ValidationError, OSError) as exc:
            failed.append((candidate, str(exc)))
    return succeeded, failed


def bulk_copy(candidates: List[str], destination_dir: str) -> Tuple[List[str], List[Tuple[str, str]]]:
    succeeded: List[str] = []
    failed: List[Tuple[str, str]] = []
    for candidate in candidates:
        try:
            copy_item(candidate, destination_dir=destination_dir)
            succeeded.append(candidate)
        except (ValidationError, OSError) as exc:
            failed.append((candidate, str(exc)))
    return succeeded, failed


def create_archive(candidates: List[str], parent_candidate, archive_name: str) -> Path:
    """Zip one or more files/folders, all from the same directory, together."""
    parent = resolve(parent_candidate)
    if not parent.is_dir():
        raise ValidationError("That is not a directory.")
    if not candidates:
        raise ValidationError("Nothing selected to archive.")

    archive_name = validate_filename(archive_name)
    if not archive_name.lower().endswith(".zip"):
        archive_name += ".zip"
    destination = resolve(parent / archive_name)
    if destination.exists():
        raise ValidationError(f"'{archive_name}' already exists.")

    sources = [resolve(c) for c in candidates]

    total = 0
    members: List[Tuple[Path, Path]] = []
    for source in sources:
        if not source.exists():
            continue
        if source.is_dir():
            for path in source.rglob("*"):
                if path.is_file():
                    total += path.stat().st_size
                    members.append((path, path.relative_to(parent)))
        else:
            total += source.stat().st_size
            members.append((source, source.relative_to(parent)))
        if total > MAX_ARCHIVE_INPUT_BYTES:
            raise ValidationError(
                f"Selection is larger than {format_size(MAX_ARCHIVE_INPUT_BYTES)}; "
                "archive something smaller."
            )

    owner = _owner_of(parent)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as zf:
        for absolute, arcname in members:
            zf.write(absolute, arcname=str(arcname))
    _restore_owner(destination, owner)
    return destination


def extract_archive(candidate, ctx=None) -> Path:
    """Extract a .zip into a new sibling folder named after it.

    Every member's destination is resolved through the same containment
    check the rest of this module relies on, with the extraction folder as
    the root instead of the sites root -- that is what stands between this
    and zip-slip: a crafted member path like "../../etc/cron.d/x" inside the
    archive must never be allowed to land outside the folder being extracted
    into, and a naive ``ZipFile.extractall()`` does not check for that at all.

    ``ctx`` is the job's :class:`~app.jobs.JobContext` when this runs as a
    background job (the normal case -- see ``tasks.extract_archive_job``);
    it is optional so the function stays directly callable from tests.
    """
    source = resolve(candidate)
    if not source.is_file() or not is_archive_name(source.name):
        raise ValidationError("That is not a .zip archive.")

    parent = source.parent
    dest_name = source.stem or "archive"
    destination = parent / dest_name
    counter = 2
    while destination.exists():
        destination = parent / f"{dest_name}-{counter}"
        counter += 1
    destination = resolve(destination)

    try:
        with zipfile.ZipFile(source) as zf:
            infos = zf.infolist()
            total = len(infos)

            extracted_size = sum(info.file_size for info in infos)
            free_bytes = shutil.disk_usage(parent).free
            if extracted_size > free_bytes - EXTRACT_FREE_SPACE_MARGIN_BYTES:
                headroom = max(free_bytes - EXTRACT_FREE_SPACE_MARGIN_BYTES, 0)
                raise ValidationError(
                    f"Archive would extract to {format_size(extracted_size)}, "
                    f"but only {format_size(headroom)} is free on disk."
                )

            if ctx is not None:
                ctx.log(f"Extracting {total} entries from {source.name}...")
                ctx.progress(0, total)

            destination.mkdir(parents=True, exist_ok=False)
            for index, info in enumerate(infos, start=1):
                member_path = resolve_within(destination, info.filename)
                if info.is_dir():
                    member_path.mkdir(parents=True, exist_ok=True)
                else:
                    member_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(member_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                if ctx is not None:
                    ctx.progress(index, total)

            if ctx is not None:
                ctx.log(f"Extracted {total} entries.")
    except zipfile.BadZipFile:
        raise ValidationError("That file is not a valid zip archive.") from None

    _restore_owner_recursive(destination, _owner_of(parent))
    return destination


def is_archive_name(name: str) -> bool:
    return Path(name).suffix.lower() in ARCHIVE_EXTENSIONS


# --------------------------------------------------------------------------
# Chunked uploads
# --------------------------------------------------------------------------


@dataclass
class _UploadSession:
    id: str
    parent: Path
    final_name: str
    part_path: Path
    total_size: int
    owner: Optional[Tuple[int, int]]
    received_bytes: int = 0
    next_index: int = 0
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_upload_sessions: Dict[str, _UploadSession] = {}


def _discard_upload_session(upload_id: str) -> None:
    session = _upload_sessions.pop(upload_id, None)
    if session is None:
        return
    try:
        session.part_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("could not remove partial upload %s: %s", session.part_path, exc)


def _sweep_expired_upload_sessions() -> None:
    now = datetime.now(timezone.utc)
    expired = [
        upload_id
        for upload_id, session in _upload_sessions.items()
        if now - session.last_activity > UPLOAD_SESSION_TTL
    ]
    for upload_id in expired:
        _discard_upload_session(upload_id)


def init_upload(parent_candidate, filename: str, total_size: int) -> _UploadSession:
    # Called on every new upload, so this is also where abandoned sessions
    # (tab closed mid-upload with no explicit cancel) get cleaned up, without
    # needing a background thread.
    _sweep_expired_upload_sessions()

    filename = validate_filename(filename)
    if total_size < 0 or total_size > MAX_UPLOAD_BYTES:
        raise ValidationError(f"Uploads are limited to {format_size(MAX_UPLOAD_BYTES)}.")

    parent = resolve(parent_candidate)
    if not parent.is_dir():
        raise ValidationError("Upload target is not a directory.")

    free_bytes = shutil.disk_usage(parent).free
    if total_size > free_bytes - UPLOAD_FREE_SPACE_MARGIN_BYTES:
        headroom = max(free_bytes - UPLOAD_FREE_SPACE_MARGIN_BYTES, 0)
        raise ValidationError(
            f"'{filename}' is {format_size(total_size)}, but only "
            f"{format_size(headroom)} is free on disk."
        )

    upload_id = uuid4().hex
    part_path = parent / f"{CHUNK_UPLOAD_PREFIX}{upload_id}{CHUNK_UPLOAD_SUFFIX}"
    part_path.write_bytes(b"")

    session = _UploadSession(
        id=upload_id,
        parent=parent,
        final_name=filename,
        part_path=part_path,
        total_size=total_size,
        owner=_owner_of(parent),
    )
    _upload_sessions[upload_id] = session
    return session


def append_chunk(upload_id: str, index: int, data: bytes) -> int:
    session = _upload_sessions.get(upload_id)
    if session is None:
        raise ValidationError("Upload session not found or has expired.")

    if index < session.next_index:
        # Already applied -- a client retry after a dropped response, not a
        # real problem. Report success without writing it twice.
        return session.received_bytes
    if index != session.next_index:
        raise ValidationError("Upload chunks arrived out of order.")
    if session.received_bytes + len(data) > session.total_size:
        raise ValidationError("Upload received more data than expected.")

    with open(session.part_path, "ab") as fh:
        fh.write(data)

    session.received_bytes += len(data)
    session.next_index += 1
    session.last_activity = datetime.now(timezone.utc)
    return session.received_bytes


def complete_upload(upload_id: str) -> Path:
    session = _upload_sessions.get(upload_id)
    if session is None:
        raise ValidationError("Upload session not found or has expired.")
    if session.received_bytes != session.total_size:
        raise ValidationError("Upload is incomplete.")

    target = resolve(session.parent / session.final_name)
    session.part_path.rename(target)
    # Files uploaded through the panel are written by root; without this the
    # site's own user could not modify what it just received.
    _restore_owner(target, session.owner)
    _upload_sessions.pop(upload_id, None)
    return target


def abort_upload(upload_id: str) -> None:
    _discard_upload_session(upload_id)


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


def _restore_owner_recursive(path: Path, owner) -> None:
    """Like ``_restore_owner``, but for a whole tree.

    ``copytree``/zip extraction create files as root (the daemon's own
    user); without walking the result, a site's own user would be unable to
    touch anything the panel just copied or extracted for it.
    """
    if not owner:
        return
    _restore_owner(path, owner)
    if path.is_dir() and not path.is_symlink():
        for child in path.rglob("*"):
            _restore_owner(child, owner)


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
