"""Let's Encrypt certificates via certbot."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from app.providers.base import Provider, register
from app.services import apt
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

    def issue(self, ctx, domain: str, *, email: Optional[str] = None, include_www: bool = True,
              staging: bool = False) -> None:
        """Obtain a certificate, leaving the vhost alone.

        ``certonly --webroot`` rather than ``--nginx`` on purpose: the nginx
        plugin rewrites the vhost to add TLS, which would fight the panel for
        ownership of a file it re-renders. Instead certbot only fetches the
        certificate, and the panel renders the SSL server block itself.

        Every value reaching the command line is validated first and passed as
        a separate argument -- never interpolated into a string.
        """
        domain = validate_domain(domain)
        if not self.is_installed():
            raise ValidationError("certbot is not installed. Install it from the Stack page.")

        ACME_ROOT.mkdir(parents=True, exist_ok=True)

        args = ["certbot", "certonly", "--webroot", "-w", str(ACME_ROOT), "-d", domain]
        if include_www:
            args += ["-d", f"www.{domain}"]
        args += ["--non-interactive", "--agree-tos", "--keep-until-expiring"]

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

    def revoke(self, ctx, domain: str) -> None:
        domain = validate_domain(domain)
        ctx.run(["certbot", "delete", "--cert-name", domain, "--non-interactive"], timeout=120)

    def certificates(self) -> List[str]:
        if not LIVE_DIR.is_dir():
            return []
        return sorted(p.name for p in LIVE_DIR.iterdir() if (p / "fullchain.pem").exists())
