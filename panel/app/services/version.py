"""Version detection and release checking for Lite-Panel.

Checks local git state / version file and queries GitHub releases for updates.
Results are cached in-memory so requests remain fast and do not exceed API limits.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Optional
from urllib.request import Request, urlopen

from app import __version__
from app.config import get_settings
from app.shell import run

logger = logging.getLogger(__name__)

GITHUB_REPO = "takshaktiwari/lite-panel"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
CACHE_TTL_SECONDS = 3600  # 1 hour


@dataclass
class VersionInfo:
    current_version: str
    git_commit: str
    latest_version: str
    update_available: bool
    release_name: str
    release_notes: str
    published_at: str
    release_url: str
    checked_at: float
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


_cached_info: Optional[VersionInfo] = None
_last_check_time: float = 0.0


def _parse_version_tuple(v: str) -> tuple:
    """Turn 'v1.2.3' or '1.2.3' into a comparable tuple of ints, e.g. (1, 2, 3)."""
    cleaned = re.sub(r"^[^\d]*", "", v.strip())
    parts = []
    for piece in cleaned.split("."):
        digits = re.match(r"^(\d+)", piece)
        if digits:
            parts.append(int(digits.group(1)))
        else:
            break
    return tuple(parts) if parts else (0,)


def is_newer_version(latest: str, current: str) -> bool:
    """Return True if `latest` is strictly newer than `current`."""
    t_latest = _parse_version_tuple(latest)
    t_current = _parse_version_tuple(current)
    return t_latest > t_current


def _install_dir_cwd() -> str:
    settings = get_settings()
    cwd = str(settings.install_dir)
    if not os.path.isdir(cwd) or not os.path.isdir(os.path.join(cwd, ".git")):
        # Fall back to repo root during development
        from app.config import REPO_ROOT
        cwd = str(REPO_ROOT)
    return cwd


def get_git_commit(cwd: Optional[str] = None) -> str:
    """Get the current commit or tag from git, or empty if not a git repo."""
    if cwd is None:
        cwd = _install_dir_cwd()

    try:
        result = run(["git", "describe", "--tags", "--always"], cwd=cwd, check=False, timeout=5)
        if result.ok:
            return result.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not get git describe: %s", exc)
    return ""


def get_current_version(cwd: Optional[str] = None) -> str:
    """Determine the running version from the checked-out git tag, falling back to
    the __version__ literal when git is unavailable (e.g. not a git checkout).

    Deriving from the tag avoids the version string drifting out of sync with what
    was actually released, which happens if __version__ isn't bumped for a tag.
    """
    if cwd is None:
        cwd = _install_dir_cwd()

    try:
        result = run(["git", "describe", "--tags", "--abbrev=0"], cwd=cwd, check=False, timeout=5)
        if result.ok:
            tag = result.stdout.strip()
            if tag and _parse_version_tuple(tag) != (0,):
                return tag
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not get nearest git tag: %s", exc)
    return __version__


def fetch_github_releases() -> list:
    """Fetch releases from GitHub API without requiring external dependencies."""
    req = Request(
        GITHUB_API_URL,
        headers={
            "User-Agent": f"lite-panel/{__version__}",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    with urlopen(req, timeout=8) as resp:  # noqa: S310 - trusted HTTPS endpoint
        return json.loads(resp.read().decode("utf-8"))


def check_for_updates(force: bool = False) -> VersionInfo:
    """Check for new release tags on GitHub. Cached for CACHE_TTL_SECONDS."""
    global _cached_info, _last_check_time

    now = time.time()
    if not force and _cached_info is not None and (now - _last_check_time) < CACHE_TTL_SECONDS:
        return _cached_info

    current = get_current_version()
    commit = get_git_commit()
    latest_tag = current
    release_name = ""
    release_notes = ""
    published_at = ""
    release_url = f"https://github.com/{GITHUB_REPO}/releases"
    error_msg = None

    try:
        releases = fetch_github_releases()
        if isinstance(releases, list) and releases:
            latest_rel = releases[0]
            latest_tag = latest_rel.get("tag_name", current)
            release_name = latest_rel.get("name") or latest_tag
            release_notes = latest_rel.get("body") or ""
            published_at = latest_rel.get("published_at") or ""
            release_url = latest_rel.get("html_url") or release_url
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to check for lite-panel updates: %s", exc)
        error_msg = str(exc)

    update_available = is_newer_version(latest_tag, current)

    info = VersionInfo(
        current_version=current,
        git_commit=commit,
        latest_version=latest_tag,
        update_available=update_available,
        release_name=release_name,
        release_notes=release_notes,
        published_at=published_at,
        release_url=release_url,
        checked_at=now,
        error=error_msg,
    )

    _cached_info = info
    _last_check_time = now
    return info


def clear_cache() -> None:
    """Reset the in-memory cache, useful for tests."""
    global _cached_info, _last_check_time
    _cached_info = None
    _last_check_time = 0.0
