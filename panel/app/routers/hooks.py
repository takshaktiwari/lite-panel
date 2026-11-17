"""Public webhook endpoint: GitHub / GitLab call this on every push.

No session and no CSRF token -- the caller is a git host, not a browser.  It is
protected instead by an unguessable per-site URL *and* a shared secret: GitHub
signs the body (``X-Hub-Signature-256``, HMAC-SHA256), GitLab sends the secret
as ``X-Gitlab-Token``.  Anything unsigned is refused, so a leaked URL alone
cannot trigger a deploy.  Only pushes to the configured branch deploy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.database import get_session
from app.models import AuditLog, SiteDeployment

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hooks")

MAX_BODY = 5 * 1024 * 1024


def signature_ok(secret: str, body: bytes, headers) -> bool:
    github = headers.get("x-hub-signature-256")
    if github:
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(github.strip(), expected)
    gitlab = headers.get("x-gitlab-token")
    if gitlab:
        return hmac.compare_digest(gitlab.strip(), secret)
    return False


def pushed_ref(body: bytes, content_type: str) -> str:
    """The ``ref`` of a push event (JSON, or GitHub's form-encoded variant)."""
    text = body.decode("utf-8", "replace")
    if "application/x-www-form-urlencoded" in (content_type or ""):
        text = (parse_qs(text).get("payload") or ["{}"])[0]
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return ""
    return data.get("ref", "") if isinstance(data, dict) else ""


@router.post("/deploy/{token}")
async def deploy_hook(token: str, request: Request, db: OrmSession = Depends(get_session)):
    deployment = db.scalar(select(SiteDeployment).where(SiteDeployment.webhook_token == token))
    # Same answer for "no such hook" and "hook switched off": the URL is the secret.
    if deployment is None or not deployment.webhook_enabled:
        return JSONResponse({"error": "not found"}, status_code=404)

    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    if not signature_ok(deployment.webhook_secret, body, request.headers):
        logger.warning("rejected unsigned or badly signed deploy webhook for site %s", deployment.site_id)
        return JSONResponse({"error": "bad signature"}, status_code=401)

    event = request.headers.get("x-github-event") or request.headers.get("x-gitlab-event") or ""
    if event == "ping":
        return JSONResponse({"ok": True, "message": "pong"})

    ref = pushed_ref(body, request.headers.get("content-type", ""))
    wanted = f"refs/heads/{deployment.branch}"
    if ref != wanted:
        return JSONResponse({"ignored": f"push to {ref or 'unknown ref'}, deploying only {wanted}"}, status_code=202)

    from app.services.deploy import queue_deploy

    site = deployment.site
    job = queue_deploy(db, site, trigger="webhook", username="webhook")
    db.add(AuditLog(user_id=None, username="webhook", action="site.deploy", target=f"{site.domain} (push)"))
    db.commit()
    if job is None:
        # A deploy is already running; this push is deployed right after it.
        return JSONResponse({"queued": "after the running deploy"}, status_code=202)
    return JSONResponse({"queued": job.id}, status_code=202)
