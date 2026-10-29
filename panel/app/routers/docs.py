"""Built-in documentation.

Static pages rendered from templates -- no markdown pipeline, no database.
``PAGES`` is the single source of truth for the docs menu, the previous/next
links and which slugs exist, so adding a page is one entry here plus one
template under ``templates/docs/``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.deps import render, require_session

router = APIRouter(prefix="/docs")

# (group, [(slug, title, one-line summary), ...]) in menu order.
GROUPS = [
    (
        "Getting started",
        [
            ("installation", "Installation", "Install the panel on a fresh server and log in for the first time."),
            ("quick-start", "Quick start", "From a blank server to a live website in about ten minutes."),
        ],
    ),
    (
        "Hosting",
        [
            ("sites", "Sites", "Create websites, assign PHP versions, enable HTTPS and manage each site's account."),
            ("node-python", "Node.js & Python apps", "Run Node.js and Python applications behind nginx as managed services."),
            ("databases", "Databases", "MariaDB databases and users, import/export, and Adminer."),
            ("files", "File manager", "Browse, edit, upload, archive and set permissions on site files."),
            ("ftp", "FTP", "Give people FTP access to a site or to any folder."),
            ("ssh-keys", "SSH keys", "Key-based SSH access for the server and for individual sites."),
            ("cron", "Cron jobs", "Schedule commands per site or for the whole server."),
            ("backups", "Backups", "One-off and scheduled backups of site files and databases."),
        ],
    ),
    (
        "Server",
        [
            ("stack", "Stack", "Install and remove nginx, PHP, MariaDB, Node.js, Python and other components."),
            ("terminal", "Terminal", "A web shell as root or as any site's user."),
            ("monitor", "Monitor", "Live and historical CPU, memory, disk and process information."),
            ("logs", "Logs", "Read nginx, PHP, database, panel and application logs."),
            ("config-editor", "Config editor", "Edit php.ini, nginx.conf and per-site nginx overrides safely."),
            ("jobs", "Jobs", "Every change the panel makes runs as a job with a live log."),
        ],
    ),
    (
        "Security",
        [
            ("firewall", "Firewall", "Open and close ports with UFW."),
            ("malware-scan", "Malware scan", "Scan sites for malware and the server for rootkits."),
            ("fail2ban", "Fail2ban", "Block brute-force SSH attempts automatically."),
        ],
    ),
    (
        "Reference",
        [
            ("cli", "Command line & services", "The lite-panel command, systemd services and file locations."),
            ("architecture", "Architecture & security", "How the panel is built and what protects it."),
            ("updates", "Updates & uninstall", "Keep the panel current, or remove it."),
            ("troubleshooting", "Troubleshooting", "Fixes for the problems people hit most often."),
        ],
    ),
]

PAGES = [
    {"slug": slug, "title": title, "summary": summary, "group": group}
    for group, items in GROUPS
    for slug, title, summary in items
]
_BY_SLUG = {page["slug"]: page for page in PAGES}


def _nav_context(current: str = "") -> dict:
    return {"doc_groups": GROUPS, "doc_current": current}


@router.get("")
def docs_home(request: Request, session=Depends(require_session)):
    return render(
        request,
        "docs/index.html",
        session=session,
        user=session.user,
        pages=PAGES,
        **_nav_context(),
    )


@router.get("/{slug}")
def docs_page(slug: str, request: Request, session=Depends(require_session)):
    page = _BY_SLUG.get(slug)
    if page is None:
        return render(
            request,
            "error.html",
            session=session,
            user=session.user,
            message="That documentation page does not exist.",
            status_code=404,
        )

    index = PAGES.index(page)
    return render(
        request,
        f"docs/{slug}.html",
        session=session,
        user=session.user,
        page=page,
        previous_page=PAGES[index - 1] if index > 0 else None,
        next_page=PAGES[index + 1] if index < len(PAGES) - 1 else None,
        **_nav_context(slug),
    )
