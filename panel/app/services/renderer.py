"""Rendering database state into the config files the panel owns.

Two rules make this safe to run at any time:

1. The panel writes *only* files it owns -- its own drop-ins and its own
   ``lite-panel-`` prefixed vhosts -- and never edits a vendor file in place.
   Nothing the operator wrote by hand can be clobbered.
2. Every render is idempotent and atomic.  Files are written to a temporary
   path and renamed over the target, so a crash mid-write cannot leave nginx
   or PHP reading half a config.

Together they mean ``lite-panel rebuild`` can reconstruct the server's
configuration from the database at any point, which is the property that makes
drift recoverable.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

BANNER = "Managed by lite-panel. Changes here are overwritten on the next render."


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(settings.assets_dir / "templates")),
        # Fail loudly on a missing variable rather than silently writing a
        # config with a blank where a socket path should be.
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        # These render config files, not HTML; escaping would corrupt them.
        autoescape=False,
    )
    env.globals["banner"] = BANNER
    return env


_env: Optional[Environment] = None


def environment() -> Environment:
    global _env
    if _env is None:
        _env = _environment()
    return _env


def render(template_name: str, context: Dict) -> str:
    """Render a template to a string."""
    return environment().get_template(template_name).render(**context)


def render_to_file(
    template_name: str,
    target: Path,
    context: Dict,
    *,
    mode: int = 0o644,
) -> bool:
    """Render a template onto disk atomically.

    Returns True if the file's contents changed, so callers can skip a service
    reload when nothing actually moved.
    """
    target = Path(target)
    content = render(template_name, context)

    if target.exists():
        try:
            if target.read_text(encoding="utf-8") == content:
                logger.debug("%s is already up to date", target)
                return False
        except (OSError, UnicodeDecodeError):
            pass

    write_atomic(target, content, mode=mode)
    logger.info("rendered %s", target)
    return True


def write_atomic(target: Path, content: str, *, mode: int = 0o644) -> None:
    """Write via a temporary file in the same directory, then rename.

    Same directory matters: rename is only atomic within a filesystem.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def remove_file(target: Path) -> bool:
    target = Path(target)
    if target.exists() or target.is_symlink():
        target.unlink()
        logger.info("removed %s", target)
        return True
    return False


# --------------------------------------------------------------------------
# Full rebuild
# --------------------------------------------------------------------------


def rebuild_all(ctx=None) -> List[str]:
    """Re-render every panel-owned file from the database.

    The recovery path: run this after editing something by hand, after a
    restore, or after resizing the machine so tuning is recalculated.
    """
    from sqlalchemy import select

    from app.database import session_scope
    from app.models import Site
    from app.providers import all_providers
    from app.services import sites as sites_service

    written: List[str] = []

    def note(message: str) -> None:
        written.append(message)
        if ctx:
            ctx.log(message)

    for provider in all_providers():
        if not provider.is_installed():
            continue
        try:
            provider.render_config(ctx)
            note(f"re-rendered {provider.name} configuration")
        except Exception as exc:  # noqa: BLE001 - one provider must not stop the rest
            note(f"warning: could not render {provider.name}: {exc}")

    with session_scope() as db:
        for site in db.scalars(select(Site)).all():
            try:
                sites_service.render_site(site)
                note(f"re-rendered site {site.domain}")
            except Exception as exc:  # noqa: BLE001
                note(f"warning: could not render {site.domain}: {exc}")

    return written
