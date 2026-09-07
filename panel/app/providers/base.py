"""The provider interface.

A provider knows how to install, remove and report on one stack component.
Keeping that behind a uniform interface is what makes the stack a *managed
resource* rather than a set of choices frozen into an installer: the panel can
offer whatever a provider says is available on this machine today.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.services import system

logger = logging.getLogger(__name__)


@dataclass
class ProviderStatus:
    key: str
    name: str
    installed: bool
    versions: List[str] = field(default_factory=list)
    service_active: bool = False
    service_installed: bool = False
    detail: str = ""


class Provider(ABC):
    """One installable stack component."""

    #: Stable identifier, used in URLs, job payloads and the database.
    key: str = ""
    #: Human name for the UI.
    name: str = ""
    #: One line explaining what installing this gets you.
    description: str = ""
    #: True when several versions can be installed side by side (PHP).
    multi_version: bool = False
    #: systemd unit, when the component has one.
    service_name: Optional[str] = None

    # -- discovery ---------------------------------------------------------

    def available_versions(self) -> List[str]:
        """Versions installable on this machine, discovered at runtime."""
        return []

    @abstractmethod
    def installed_versions(self) -> List[str]:
        """Versions currently installed.  Empty means not installed."""

    def is_installed(self) -> bool:
        return bool(self.installed_versions())

    # -- mutation ----------------------------------------------------------

    @abstractmethod
    def install(self, ctx, version: Optional[str] = None) -> None:
        """Install the component.  Runs inside a job; use ``ctx`` to log."""

    @abstractmethod
    def uninstall(self, ctx, version: Optional[str] = None) -> None:
        """Remove the component."""

    def render_config(self, ctx=None) -> None:
        """Write the panel-owned drop-in files for this component.

        Providers that own no config leave this alone.  Implementations must
        be idempotent: ``lite-panel rebuild`` calls every one of them.
        """

    # -- reporting ---------------------------------------------------------

    def status(self) -> ProviderStatus:
        service = (
            system.service_state(self.service_name)
            if self.service_name
            else system.ServiceState(name="", installed=False, active=False)
        )
        return ProviderStatus(
            key=self.key,
            name=self.name,
            installed=self.is_installed(),
            versions=self.installed_versions(),
            service_active=service.active,
            service_installed=service.installed,
            detail=service.detail,
        )

    def restart_service(self, ctx) -> None:
        if self.service_name:
            ctx.check(["systemctl", "restart", self.service_name], timeout=120)

    def reload_service(self, ctx) -> None:
        if self.service_name:
            ctx.check(["systemctl", "reload-or-restart", self.service_name], timeout=120)

    def enable_service(self, ctx) -> None:
        if self.service_name:
            ctx.check(["systemctl", "enable", "--now", self.service_name], timeout=120)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

_registry: Dict[str, Provider] = {}


def register(provider_cls):
    """Class decorator adding a provider to the registry."""
    instance = provider_cls()
    if not instance.key:
        raise RuntimeError(f"{provider_cls.__name__} has no key")
    if instance.key in _registry:
        raise RuntimeError(f"provider '{instance.key}' is already registered")
    _registry[instance.key] = instance
    return provider_cls


def get_provider(key: str) -> Provider:
    try:
        return _registry[key]
    except KeyError:
        raise KeyError(f"unknown provider '{key}'") from None


def all_providers() -> List[Provider]:
    return [_registry[key] for key in sorted(_registry)]


def provider_keys() -> List[str]:
    return sorted(_registry)
