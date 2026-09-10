"""Sites: create, configure, SSL, delete."""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession, selectinload

from app.database import get_session
from app.deps import csrf_protect, job_redirect, render, require_session
from app.jobs import enqueue
from app.models import AuditLog, FtpAccount, Site, SiteDatabase, SshKey
from app.providers import get_provider
from app.services import dns_check as dns_check_service
from app.services import sites as sites_service
from app.services import system as system_service
from app.validators import ValidationError, validate_domain, validate_site_name

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sites")


@router.get("")
def site_list(
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    sites = db.scalars(
        select(Site).options(selectinload(Site.databases)).order_by(Site.name)
    ).all()
    return render(
        request,
        "sites/list.html",
        session=session,
        user=session.user,
        sites=sites,
        php_versions=get_provider("php").installed_versions(),
        nginx_ready=get_provider("nginx").is_installed(),
    )


@router.get("/new")
def new_site_form(
    request: Request,
    session=Depends(require_session),
):
    return render(
        request,
        "sites/new.html",
        session=session,
        user=session.user,
        php_versions=get_provider("php").installed_versions(),
        nginx_ready=get_provider("nginx").is_installed(),
        certbot_ready=get_provider("certbot").is_installed(),
        sites_root=str(sites_service.sites_root()),
    )


@router.post("/new", dependencies=[Depends(csrf_protect)])
def create_site(
    request: Request,
    domain: str = Form(...),
    folder: str = Form(""),
    php_version: str = Form(""),
    redirect_www: str = Form(""),
    enable_ssl: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    php = get_provider("php")

    try:
        domain = validate_domain(domain)
        # One field, typed right after the /var/www/ prefix shown in the
        # form: the first segment is the site's own folder (and where its
        # system user's account lives); anything past the first "/" is an
        # optional extra nesting for the webroot (e.g. "blog/public") --
        # most sites don't need it, so it's opt-in rather than a forced
        # default.
        name, subfolder = _split_folder(folder, domain)
    except ValidationError as exc:
        return render(
            request,
            "sites/new.html",
            session=session,
            user=session.user,
            php_versions=php.installed_versions(),
            nginx_ready=get_provider("nginx").is_installed(),
            certbot_ready=get_provider("certbot").is_installed(),
            sites_root=str(sites_service.sites_root()),
            error=str(exc),
            form={"domain": domain, "folder": folder},
            status_code=400,
        )

    if db.scalar(select(Site).where(Site.domain == domain)):
        return _back(error=f"{domain} is already served by another site.")
    if db.scalar(select(Site).where(Site.name == name)):
        return _back(error=f"A site named '{name}' already exists.")

    job = enqueue(
        db,
        "site.create",
        f"Create site {domain}",
        payload={
            "domain": domain,
            "name": name,
            "php_version": php_version.strip() or None,
            "subfolder": subfolder,
            "redirect_www": bool(redirect_www),
            "enable_ssl": bool(enable_ssl),
        },
        user_id=session.user_id,
    )
    _audit(db, session, "site.create", domain)
    return job_redirect(job.id, "/sites")


@router.get("/{site_id}")
def site_detail(
    site_id: int,
    request: Request,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _not_found(request, session)

    certbot = get_provider("certbot")
    return render(
        request,
        "sites/detail.html",
        session=session,
        user=session.user,
        site=site,
        php_versions=get_provider("php").installed_versions(),
        databases=db.scalars(select(SiteDatabase).where(SiteDatabase.site_id == site.id)).all(),
        ftp_account=db.scalar(select(FtpAccount).where(FtpAccount.site_id == site.id)),
        ssh_keys=db.scalars(select(SshKey).where(SshKey.site_id == site.id)).all(),
        certbot_ready=certbot.is_installed(),
        has_certificate=certbot.has_certificate(site.domain) if certbot.is_installed() else False,
        socket=str(sites_service.socket_for(site.name)) if site.php_version else None,
        webroot_subfolder=sites_service.relative_webroot(site),
    )


@router.get("/{site_id}/dns-check")
def dns_check(
    site_id: int,
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    """On-demand fan-out to public resolvers, fetched by the detail page's button."""
    site = db.get(Site, site_id)
    if site is None:
        return JSONResponse({"error": "That site no longer exists."}, status_code=404)

    expected_ip = system_service.public_ip()
    results = dns_check_service.check_propagation(site.domain, expected_ip)
    return JSONResponse({
        "domain": site.domain,
        "expected_ip": expected_ip,
        "resolvers": [
            {
                "label": r.label,
                "resolver_ip": r.resolver_ip,
                "region": r.region,
                "answers": r.answers,
                "matches": r.matches,
                "error": r.error,
            }
            for r in results
        ],
    })


@router.post("/{site_id}/php", dependencies=[Depends(csrf_protect)])
def change_php(
    site_id: int,
    php_version: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _back(error="That site no longer exists.")

    job = enqueue(
        db,
        "site.php_version",
        f"Change {site.domain} to PHP {php_version or 'none'}",
        payload={"site_id": site.id, "php_version": php_version.strip() or None},
        user_id=session.user_id,
    )
    _audit(db, session, "site.php_version", f"{site.domain} -> {php_version or 'none'}")
    return job_redirect(job.id, f"/sites/{site.id}")


@router.post("/{site_id}/webroot", dependencies=[Depends(csrf_protect)])
def change_webroot(
    site_id: int,
    webroot: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _back(error="That site no longer exists.")

    try:
        subfolder = _clean_subfolder(webroot)
    except ValidationError as exc:
        return RedirectResponse(f"/sites/{site.id}?error={quote(str(exc))}", status_code=303)

    job = enqueue(
        db,
        "site.webroot",
        f"Change document path for {site.domain}",
        payload={"site_id": site.id, "subfolder": subfolder},
        user_id=session.user_id,
    )
    _audit(db, session, "site.webroot", f"{site.domain} -> /{subfolder}" if subfolder else f"{site.domain} -> (site root)")
    return job_redirect(job.id, f"/sites/{site.id}")


@router.post("/{site_id}/ssl", dependencies=[Depends(csrf_protect)])
def toggle_ssl(
    site_id: int,
    action: str = Form(...),
    email: str = Form(""),
    staging: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _back(error="That site no longer exists.")

    if action == "disable":
        job = enqueue(
            db,
            "site.ssl_disable",
            f"Disable HTTPS for {site.domain}",
            payload={"site_id": site.id},
            user_id=session.user_id,
        )
    else:
        if not get_provider("certbot").is_installed():
            return RedirectResponse(
                f"/sites/{site.id}?error={quote('Install SSL support from the Stack page first.')}",
                status_code=303,
            )
        job = enqueue(
            db,
            "site.ssl_enable",
            f"Enable HTTPS for {site.domain}",
            payload={
                "site_id": site.id,
                "email": email.strip() or None,
                "staging": bool(staging),
            },
            user_id=session.user_id,
        )

    _audit(db, session, f"site.ssl_{action}", site.domain)
    return job_redirect(job.id, f"/sites/{site.id}")


@router.post("/{site_id}/delete", dependencies=[Depends(csrf_protect)])
def delete_site(
    site_id: int,
    confirm: str = Form(""),
    remove_files: str = Form(""),
    delete_databases: str = Form(""),
    session=Depends(require_session),
    db: OrmSession = Depends(get_session),
):
    site = db.get(Site, site_id)
    if site is None:
        return _back(error="That site no longer exists.")

    # Typing the domain is the confirmation: a delete that removes files is
    # not recoverable, and the button alone is too easy to hit.
    if confirm.strip().lower() != site.domain:
        return RedirectResponse(
            f"/sites/{site.id}?error={quote('Type the domain exactly to confirm deletion.')}",
            status_code=303,
        )

    job = enqueue(
        db,
        "site.delete",
        f"Delete site {site.domain}",
        payload={
            "site_id": site.id,
            "remove_files": bool(remove_files),
            "delete_databases": bool(delete_databases),
        },
        user_id=session.user_id,
    )
    _audit(db, session, "site.delete", site.domain)
    return job_redirect(job.id, "/sites")


# --------------------------------------------------------------------------


def _name_from_domain(domain: str) -> str:
    """Derive a slug from a domain: blog.example.com -> blog-example."""
    parts = domain.split(".")
    stem = "-".join(parts[:-1]) if len(parts) > 1 else domain
    cleaned = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in stem.lower())
    return cleaned.strip("-")[:32] or "site"


def _split_folder(folder: str, domain: str) -> tuple:
    """Split what was typed after the /var/www/ prefix into (name, subfolder).

    The first path segment becomes the site's own folder (and the name its
    system user is derived from); anything after the first "/" is an
    optional extra nesting for the webroot, e.g. typing "blog/public" serves
    from /var/www/blog/public while the site itself still lives at
    /var/www/blog. Left blank entirely, the name falls back to one derived
    from the domain and there is no subfolder -- most sites don't need one.
    """
    cleaned = folder.strip().strip("/")
    if not cleaned:
        return validate_site_name(_name_from_domain(domain)), ""

    first, _, rest = cleaned.partition("/")
    name = validate_site_name(first)
    return name, _clean_subfolder(rest)


def _clean_subfolder(value: str) -> str:
    """Validate a webroot path typed relative to a site's home directory.

    Segment-by-segment, like the file manager's containment check, so the
    result can never climb outside root_dir via "..".
    """
    from app.validators import validate_filename

    cleaned = value.strip().strip("/")
    if not cleaned:
        return ""
    segments = [s for s in cleaned.split("/") if s]
    for segment in segments:
        validate_filename(segment)
    return "/".join(segments)


def _back(*, error: Optional[str] = None):
    suffix = f"?error={quote(error)}" if error else ""
    return RedirectResponse(f"/sites{suffix}", status_code=303)


def _not_found(request: Request, session):
    return render(
        request,
        "error.html",
        session=session,
        user=session.user,
        message="That site does not exist.",
        status_code=404,
    )


def _audit(db: OrmSession, session, action: str, target: Optional[str]) -> None:
    db.add(
        AuditLog(
            user_id=session.user_id,
            username=session.user.username,
            action=action,
            target=target,
        )
    )
    db.commit()
