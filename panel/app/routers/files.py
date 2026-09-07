"""The file manager.

Every handler resolves its path through :mod:`app.services.files`, which
refuses anything outside the sites root.  There is no code path here that
touches the filesystem without that check.
"""

from __future__ import annotations

import logging
from typing import Optional
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


# --------------------------------------------------------------------------


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
