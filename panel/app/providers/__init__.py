"""Stack components the panel can install and manage.

Each component implements the same small interface, so adding Apache or
PostgreSQL later is a new file here rather than a change to the pages,
routers or job system that consume them.
"""

from app.providers.base import Provider, ProviderStatus, all_providers, get_provider  # noqa: F401

# Importing the modules is what registers them.
from app.providers import certbot, mariadb, nginx, php, vsftpd  # noqa: F401,E402

__all__ = ["Provider", "ProviderStatus", "all_providers", "get_provider"]
