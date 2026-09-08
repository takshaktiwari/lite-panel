"""Runtime configuration.

Settings come from environment variables prefixed with ``LITE_PANEL_``, loaded
from ``/etc/lite-panel/panel.env`` on a real install.  Every filesystem path is
overridable so the pure-Python parts of the panel (validators, renderer, auth)
can be developed and tested on a workstation without touching system paths.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

# panel/app/config.py -> panel/app -> panel -> <repo root>
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LITE_PANEL_",
        env_file="/etc/lite-panel/panel.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "lite-panel"

    # Networking.  The daemon must never bind a public interface: nginx
    # terminates TLS and enforces the session gate in front of it.
    host: str = "127.0.0.1"
    port: int = 8765
    adminer_port: int = 8766

    # Filesystem layout on an installed server.
    install_dir: Path = Path("/opt/lite-panel")
    state_dir: Path = Path("/var/lib/lite-panel")
    config_dir: Path = Path("/etc/lite-panel")
    log_dir: Path = Path("/var/log/lite-panel")

    # Root under which site directories live.  Also the containment boundary
    # for the file manager: nothing outside this tree is reachable.
    sites_root: Path = Path("/home")

    # Signing/session secret.  install.sh generates one; a blank value in
    # dev_mode gets a throwaway random key at startup instead of failing.
    secret_key: str = ""

    session_ttl_hours: int = 12
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15

    # The web terminal closes itself after this long with no client input.
    terminal_idle_timeout_minutes: int = 20

    # Set by dev tooling; relaxes the checks that only make sense on a server.
    dev_mode: bool = False

    # Overridden in tests to point at a scratch file.
    database_url: Optional[str] = None

    @property
    def db_path(self) -> Path:
        return self.state_dir / "panel.db"

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.db_path}"

    @property
    def assets_dir(self) -> Path:
        """Config templates and systemd units shipped with the panel."""
        repo_assets = REPO_ROOT / "assets"
        return repo_assets if repo_assets.is_dir() else self.install_dir / "assets"

    @property
    def web_templates_dir(self) -> Path:
        return PACKAGE_DIR / "templates"

    @property
    def static_dir(self) -> Path:
        return PACKAGE_DIR / "static"


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()
