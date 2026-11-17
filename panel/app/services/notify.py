"""Send a short alert to a chat / generic webhook (Slack, Discord, Mattermost...).

The URL is typed by an admin but the *panel* makes the request, so it must not
be usable to poke at things only the server can reach: only https, only public
addresses (checked on the resolved IPs, so a DNS name pointing at 127.0.0.1 is
refused too), and redirects are never followed.

One JSON body works for the common services: ``text`` is Slack's / Mattermost's
field and ``content`` is Discord's; each ignores the other.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10


class NotifyError(RuntimeError):
    """The alert could not be delivered; the message says why."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib API
        return None


def _public_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise NotifyError(f"Cannot resolve {host}: {exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise NotifyError(f"{host} points to a private address ({address}); alerts go to public https hosts only.")


def send(url: str, text: str, *, event: str = "", site: str = "", extra=None) -> None:
    parts = urlsplit(url or "")
    if parts.scheme != "https" or not parts.hostname:
        raise NotifyError("The alert webhook must be an https:// URL.")
    _public_host(parts.hostname)

    body = {"text": text, "content": text, "event": event, "site": site, **(extra or {})}
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "lite-panel"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            if not 200 <= response.status < 300:
                raise NotifyError(f"The webhook answered HTTP {response.status}.")
    except urllib.error.HTTPError as exc:
        raise NotifyError(f"The webhook answered HTTP {exc.code}.") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise NotifyError(f"Could not reach the webhook: {getattr(exc, 'reason', exc)}") from exc
