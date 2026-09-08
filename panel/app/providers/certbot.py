"""Let's Encrypt certificates via certbot."""

from __future__ import annotations

import logging
import socket
from pathlib import Path
from typing import List, Optional

from app.providers.base import Provider, register
from app.services import apt
from app.shell import CommandError
from app.shell import run as shell_run
from app.validators import ValidationError, validate_domain

logger = logging.getLogger(__name__)

LIVE_DIR = Path("/etc/letsencrypt/live")

# One shared directory for HTTP-01 challenges. Every site's vhost serves
# /.well-known/acme-challenge/ from here, so issuing and renewing a
# certificate never depends on the site's own document root being writable.
ACME_ROOT = Path("/var/www/lite-panel-acme")


@register
class CertbotProvider(Provider):
    key = "certbot"
    name = "SSL (Let's Encrypt)"
    description = "Free HTTPS certificates, renewed automatically."
    service_name = None

    def installed_versions(self) -> List[str]:
        version = apt.installed_version("certbot")
        return [version] if version else []

    def install(self, ctx, version: Optional[str] = None) -> None:
        if self.is_installed():
            ctx.log("certbot is already installed")
            return
        ctx.log("Installing certbot")
        apt.install(ctx, ["certbot"])
        ACME_ROOT.mkdir(parents=True, exist_ok=True)
        self._install_renewal_hook(ctx)
        ctx.log("certbot installed; renewal runs from its own systemd timer")

    def _install_renewal_hook(self, ctx) -> None:
        """Reload nginx after a renewal.

        certbot renews in the background on a timer, but the running nginx
        keeps serving the certificate it loaded at startup. Without this hook
        a renewed certificate is not actually presented until something else
        happens to restart nginx -- typically noticed when it has expired.
        """
        from app.services.renderer import write_atomic

        hook = Path("/etc/letsencrypt/renewal-hooks/deploy/lite-panel-reload-nginx.sh")
        write_atomic(
            hook,
            "#!/bin/sh\n"
            "# Managed by lite-panel.\n"
            "systemctl reload nginx 2>/dev/null || true\n",
            mode=0o755,
        )
        ctx.log(f"installed renewal hook {hook}")

    def uninstall(self, ctx, version: Optional[str] = None) -> None:
        ctx.log("Removing certbot")
        apt.remove(ctx, ["certbot", "python3-certbot-nginx"])
        ctx.log("certbot removed; existing certificates were left in /etc/letsencrypt")

    # -- certificates ------------------------------------------------------

    def has_certificate(self, domain: str) -> bool:
        domain = validate_domain(domain)
        return (LIVE_DIR / domain / "fullchain.pem").exists()

    def covers_www(self, domain: str) -> bool:
        """Whether the certificate currently on disk for ``domain`` also covers
        ``www.<domain>``.

        Read from the certificate's own SAN list rather than any panel-side
        flag, so a rebuild (or a render triggered for an unrelated reason,
        like a PHP version change) always reflects what Let's Encrypt actually
        issued -- including the case where ``issue()`` silently dropped ``www``
        because it had no DNS record at the time.
        """
        domain = validate_domain(domain)
        cert = LIVE_DIR / domain / "cert.pem"
        if not cert.exists():
            return False
        try:
            result = shell_run(
                ["openssl", "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"],
                check=False,
                timeout=10,
            )
        except (CommandError, OSError):
            return False
        return f"DNS:www.{domain}" in result.stdout

    def issue(self, ctx, domain: str, *, email: Optional[str] = None, include_www: bool = True,
              staging: bool = False) -> bool:
        """Obtain a certificate, leaving the vhost alone.

        ``certonly --webroot`` rather than ``--nginx`` on purpose: the nginx
        plugin rewrites the vhost to add TLS, which would fight the panel for
        ownership of a file it re-renders. Instead certbot only fetches the
        certificate, and the panel renders the SSL server block itself.

        Every value reaching the command line is validated first and passed as
        a separate argument -- never interpolated into a string.

        Returns whether the ``www`` subdomain ended up in the certificate.
        ``www`` is dropped instead of failing the whole request when it has no
        DNS record yet -- a bare domain that resolves and a ``www`` that
        doesn't is the common case for a freshly created site, and it should
        still get HTTPS rather than being blocked by a subdomain nobody has
        pointed anywhere.
        """
        domain = validate_domain(domain)
        if not self.is_installed():
            raise ValidationError("certbot is not installed. Install it from the Stack page.")

        ACME_ROOT.mkdir(parents=True, exist_ok=True)

        www_domain = f"www.{domain}"
        request_www = include_www and self._resolves(www_domain)
        if include_www and not request_www:
            ctx.log(f"{www_domain} has no DNS record yet; requesting a certificate for {domain} only")

        args = ["certbot", "certonly", "--webroot", "-w", str(ACME_ROOT), "-d", domain]
        if request_www:
            args += ["-d", www_domain]
        # --expand lets a re-issue (e.g. www added after the cert already exists
        # for the bare domain) replace the existing cert instead of certbot
        # halting to ask an interactive question it can never get answered.
        args += ["--non-interactive", "--agree-tos", "--keep-until-expiring", "--expand"]

        if email:
            args += ["-m", email]
        else:
            # certbot refuses to run unattended without one or the other.
            args.append("--register-unsafely-without-email")

        if staging:
            args.append("--staging")

        ctx.log(f"Requesting a certificate for {domain}")
        code = ctx.run(args, timeout=600)
        if code != 0:
            raise RuntimeError(
                "Certificate request failed. The usual cause is DNS: "
                f"{domain} must already point at this server's public IP, and "
                "port 80 must be reachable from the internet."
            )
        ctx.log(f"Certificate installed for {domain}")
        return request_www

    @staticmethod
    def _resolves(hostname: str) -> bool:
        try:
            socket.getaddrinfo(hostname, None)
            return True
        except socket.gaierror:
            return False

    def revoke(self, ctx, domain: str) -> None:
        domain = validate_domain(domain)
        ctx.run(["certbot", "delete", "--cert-name", domain, "--non-interactive"], timeout=120)

    def certificates(self) -> List[str]:
        if not LIVE_DIR.is_dir():
            return []
        return sorted(p.name for p in LIVE_DIR.iterdir() if (p / "fullchain.pem").exists())
