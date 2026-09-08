"""The file manager.

Every handler resolves its path through :mod:`app.services.files`, which
refuses anything outside the sites root.  There is no code path here that
touches the filesystem without that check.
"""

from __future__ import annotations

import logging
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session as OrmSession

from app.database import get_session
from app.deps import csrf_protect, render, require_session
from app.models import AuditLog
from app.services import files as files_service
from app.validators import ValidationError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/files")


@router.get("")
def browse(
    request: Request,
    path: str = ".",
    session=Depends(require_session),
):
    try:
        entries = files_service.list_directory(path)
        crumbs = files_service.breadcrumbs(path)
        current = files_service.relative_to_root(files_service.resolve(path))
    except ValidationError as exc:
        return render(
            request,
            "files/browse.html",
            session=session,
            user=session.user,
            entries=[],
            crumbs=[{"name": "Home", "path": "."}],
            current=".",
            parent=".",
            error=str(exc),
            status_code=400,
        )

    return render(
        request,
        "files/browse.html",
        session=session,
        user=session.user,
        entries=entries,
        crumbs=crumbs,
        current=current,
        parent=files_service.parent_of(path),
        root=str(files_service.root()),
    )


@router.get("/edit")
def edit_form(
    request: Request,
    path: str,
    session=Depends(require_session),
):
    try:
        content = files_service.read_text(path)
    except ValidationError as exc:
        return _back(".", error=str(exc))

    resolved = files_service.resolve(path)
    return render(
        request,
        "files/edit.html",
        session=session,
        user=session.user,
        path=files_service.relative_to_root(resolved),
        name=resolved.name,
        parent=files_service.parent_of(path),
        content=content,
    )


@router.post("/edit", dependencies=[Depends(csrf_protect)])
def save_file(
    path: str = Form(...),
    content: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        files_service.write_text(path, content)
    except (ValidationError, OSError) as exc:
        return _back(".", error=str(exc))

    _audit(db, session, "files.edit", path)
    parent = files_service.parent_of(path)
    return _back(parent, notice=f"Saved {path}")


@router.get("/download")
def download(
    path: str,
    session=Depends(require_session),
):
    try:
        target = files_service.resolve(path)
    except ValidationError as exc:
        return _back(".", error=str(exc))

    if not target.is_file():
        return _back(".", error="That file does not exist.")

    return FileResponse(
        str(target),
        filename=target.name,
        # Never let the browser render a downloaded file inline: a site's
        # uploaded .html would otherwise execute in the panel's own origin.
        media_type="application/octet-stream",
    )


@router.post("/upload", dependencies=[Depends(csrf_protect)])
async def upload(
    path: str = Form(...),
    upload: UploadFile = File(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        data = await upload.read()
        saved = files_service.save_upload(path, upload.filename or "upload", data)
    except (ValidationError, OSError) as exc:
        return _back(path, error=str(exc))

    _audit(db, session, "files.upload", str(saved))
    return _back(path, notice=f"Uploaded {saved.name}")


@router.post("/mkdir", dependencies=[Depends(csrf_protect)])
def make_directory(
    path: str = Form(...),
    name: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        files_service.create_directory(path, name)
    except (ValidationError, OSError) as exc:
        return _back(path, error=str(exc))

    _audit(db, session, "files.mkdir", f"{path}/{name}")
    return _back(path, notice=f"Created {name}")


@router.post("/new-file", dependencies=[Depends(csrf_protect)])
def make_file(
    path: str = Form(...),
    name: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        files_service.create_file(path, name)
    except (ValidationError, OSError) as exc:
        return _back(path, error=str(exc))

    _audit(db, session, "files.create", f"{path}/{name}")
    return _back(path, notice=f"Created {name}")


@router.post("/rename", dependencies=[Depends(csrf_protect)])
def rename(
    path: str = Form(...),
    target: str = Form(...),
    new_name: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        files_service.rename(target, new_name)
    except (ValidationError, OSError) as exc:
        return _back(path, error=str(exc))

    _audit(db, session, "files.rename", f"{target} -> {new_name}")
    return _back(path, notice=f"Renamed to {new_name}")


@router.post("/delete", dependencies=[Depends(csrf_protect)])
def delete(
    path: str = Form(...),
    target: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        name = files_service.delete(target)
    except (ValidationError, OSError) as exc:
        return _back(path, error=str(exc))

    _audit(db, session, "files.delete", target)
    return _back(path, notice=f"Deleted {name}")


@router.post("/duplicate", dependencies=[Depends(csrf_protect)])
def duplicate(
    path: str = Form(...),
    target: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    """Copy one item in place -- the "back this up before I touch it" button
    next to Edit."""
    try:
        copied = files_service.copy_item(target)
    except (ValidationError, OSError) as exc:
        return _back(path, error=str(exc))

    _audit(db, session, "files.duplicate", f"{target} -> {copied.name}")
    return _back(path, notice=f"Created {copied.name}")


@router.post("/archive", dependencies=[Depends(csrf_protect)])
def archive_one(
    path: str = Form(...),
    target: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    """Zip a single file or folder -- the row-level Archive action."""
    from pathlib import Path as _Path

    try:
        name = _Path(target).name or "archive"
        archive = files_service.create_archive([target], path, name)
    except (ValidationError, OSError) as exc:
        return _back(path, error=str(exc))

    _audit(db, session, "files.archive", f"{target} -> {archive.name}")
    return _back(path, notice=f"Created {archive.name}")


@router.post("/extract", dependencies=[Depends(csrf_protect)])
def extract(
    path: str = Form(...),
    target: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    try:
        destination = files_service.extract_archive(target)
    except (ValidationError, OSError) as exc:
        return _back(path, error=str(exc))

    _audit(db, session, "files.extract", f"{target} -> {destination.name}")
    return _back(path, notice=f"Extracted to {destination.name}")


# --------------------------------------------------------------------------
# Bulk actions -- one shared selection of checkboxes, several possible
# destinations via each button's own formaction (see files/browse.html).
# --------------------------------------------------------------------------


@router.post("/bulk-delete", dependencies=[Depends(csrf_protect)])
def bulk_delete(
    path: str = Form(...),
    target: List[str] = Form(default=[]),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    if not target:
        return _back(path, error="Nothing was selected.")

    succeeded, failed = files_service.bulk_delete(target)
    for name in succeeded:
        _audit(db, session, "files.delete", name)

    return _back(path, **_bulk_result(succeeded, failed, verb="Deleted"))


@router.post("/bulk-copy", dependencies=[Depends(csrf_protect)])
def bulk_copy(
    path: str = Form(...),
    target: List[str] = Form(default=[]),
    destination: str = Form(...),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    if not target:
        return _back(path, error="Nothing was selected.")

    try:
        dest_resolved = files_service.resolve(destination)
        if not dest_resolved.is_dir():
            return _back(path, error="Destination is not a directory.")
    except ValidationError as exc:
        return _back(path, error=str(exc))

    succeeded, failed = files_service.bulk_copy(target, destination)
    for name in succeeded:
        _audit(db, session, "files.copy", f"{name} -> {destination}")

    return _back(path, **_bulk_result(succeeded, failed, verb="Copied"))


@router.post("/bulk-archive", dependencies=[Depends(csrf_protect)])
def bulk_archive(
    path: str = Form(...),
    target: List[str] = Form(default=[]),
    archive_name: str = Form("archive"),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    if not target:
        return _back(path, error="Nothing was selected.")

    try:
        archive = files_service.create_archive(target, path, archive_name or "archive")
    except (ValidationError, OSError) as exc:
        return _back(path, error=str(exc))

    _audit(db, session, "files.archive", f"{len(target)} item(s) -> {archive.name}")
    return _back(path, notice=f"Created {archive.name} with {len(target)} item(s)")


# --------------------------------------------------------------------------


def _bulk_result(succeeded: List[str], failed: List, *, verb: str) -> dict:
    """Turn a (succeeded, failed) pair from a bulk operation into the
    notice/error kwargs _back() expects -- reporting a partial success
    honestly rather than picking one message and hiding the other."""
    result: dict = {}
    if succeeded:
        result["notice"] = f"{verb} {len(succeeded)} item(s)."
    if failed:
        names = ", ".join(name for name, _ in failed[:3])
        more = f" and {len(failed) - 3} more" if len(failed) > 3 else ""
        result["error"] = f"Could not process: {names}{more}."
    return result


def _back(path: str, *, notice: Optional[str] = None, error: Optional[str] = None):
    url = f"/files?path={quote(path or '.')}"
    if notice:
        url += f"&notice={quote(notice)}"
    if error:
        url += f"&error={quote(error)}"
    return RedirectResponse(url, status_code=303)


def _audit(db: OrmSession, session, action: str, target: Optional[str]) -> None:
    db.add(
        AuditLog(
            user_id=session.user_id,
            username=session.user.username,
            action=action,
            target=(target or "")[:255],
        )
    )
    db.commit()
