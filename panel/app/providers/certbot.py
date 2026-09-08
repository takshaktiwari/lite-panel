"""Let's Encrypt certificates via certbot."""

from __future__ import annotations

import logging
import secrets
import socket
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional, Set, Tuple

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

# Where an HTTP-01 challenge file actually lands under ACME_ROOT. nginx serves
# this prefix with `root`, so the URI path is appended to the root directory.
CHALLENGE_SUBPATH = Path(".well-known/acme-challenge")


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

        # Everything Let's Encrypt needs is checked here first, because every
        # failed order counts against a per-hostname failed-validation limit
        # (5/hour) that blind retries burn through -- and because certbot's own
        # failure output is a wall of text that buries the one line that matters.
        self._preflight(ctx, domain)

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
            # DNS was already confirmed to resolve in _preflight, so pointing
            # at DNS first (as this message used to) would send the operator
            # to check the one thing already known to be fine.
            raise RuntimeError(
                f"Certificate request failed. {domain} does resolve, so the cause is "
                "usually reachability rather than DNS: port 80 must be open to the "
                "internet and reach this server, and any proxy in front of it (e.g. "
                "Cloudflare) must pass /.well-known/acme-challenge/ through to the "
                "origin. See the certbot output above for what the CA reported."
            )
        ctx.log(f"Certificate installed for {domain}")
        return request_www

    # -- pre-flight --------------------------------------------------------

    def _preflight(self, ctx, domain: str) -> None:
        """Check what Let's Encrypt is about to check, before asking it to.

        Two very different outcomes on purpose:

        * No DNS record at all is a hard stop. There is no arrangement of
          firewall, nginx or certbot flags under which a name that does not
          resolve can be validated, so failing here costs the operator a
          10-second wait instead of a minute plus one of five hourly
          failed-validation slots for that hostname.
        * A challenge file that cannot be fetched back is only a *warning*.
          It is the same path the CA will take, so it is worth reporting
          loudly -- but it is fetched from this box, and a server that cannot
          reach its own public hostname (hairpin NAT, split-horizon DNS, an
          egress filter) is a false alarm we must not turn into a refusal.
        """
        from app.services import system

        ips = self._resolved_ips(domain)
        if not ips:
            server_ip = system.public_ip()
            target = f" pointing to {server_ip}" if server_ip else ""
            raise ValidationError(
                f"{domain} has no DNS record, so Let's Encrypt cannot reach it to "
                f"verify the domain is yours. Add a DNS A record for {domain}"
                f"{target}, give it a minute to propagate, then enable HTTPS again. "
                "Nothing was sent to Let's Encrypt, so no rate limit was used."
            )

        ctx.log(f"{domain} resolves to {', '.join(sorted(ips))}")

        server_ip = system.public_ip()
        # Not an error: a proxied domain (Cloudflare's orange cloud) is a
        # perfectly normal setup that resolves to the proxy, not to us.
        proxied = bool(server_ip) and server_ip not in ips
        if proxied:
            ctx.log(
                f"note: {domain} does not resolve to this server ({server_ip}) -- "
                "fine if it is proxied (e.g. Cloudflare), a problem if it is not"
            )

        ok, detail = self._challenge_reachable(domain)
        if ok:
            ctx.log(f"HTTP-01 challenge path is reachable at http://{domain}")
        elif proxied:
            # Behind a proxy this check is not evidence of anything. A proxy
            # commonly answers a request coming from the origin's own address
            # differently than one arriving from the CA (Cloudflare returns
            # 403 to exactly this probe on a domain whose certificate issues
            # perfectly well), so reporting it as a warning would put a scary
            # line in the log of every working proxied site.
            ctx.log(
                f"note: the test challenge fetch returned '{detail}', which is normal "
                "for a proxied domain and says nothing about what the CA will see"
            )
        else:
            ctx.log(f"warning: could not fetch a test challenge file over http://{domain} -- {detail}")
            ctx.log("continuing anyway; Let's Encrypt reaches this server from outside, which this check cannot do")

    @staticmethod
    def _resolved_ips(hostname: str) -> Set[str]:
        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return set()
        return {info[4][0] for info in infos}

    @staticmethod
    def _challenge_reachable(domain: str) -> Tuple[bool, str]:
        """Serve a token under the ACME path and fetch it back over the public
        hostname -- the same round trip the CA makes.

        Redirects are followed and TLS is not verified, because that is what
        the CA itself does for HTTP-01: an http -> https redirect is allowed
        and the certificate on the far end is explicitly not checked (it is
        usually the very certificate being replaced).
        """
        directory = ACME_ROOT / CHALLENGE_SUBPATH
        token = f"lite-panel-preflight-{secrets.token_urlsafe(16)}"
        probe = directory / token

        try:
            directory.mkdir(parents=True, exist_ok=True)
            # Traversed and read by nginx as www-data, not by root.
            for parent in (ACME_ROOT, ACME_ROOT / ".well-known", directory):
                parent.chmod(0o755)
            probe.write_text(token, encoding="utf-8")
            probe.chmod(0o644)
        except OSError as exc:
            return False, f"could not write the test file: {exc}"

        url = f"http://{domain}/.well-known/acme-challenge/{token}"
        unverified = ssl.create_default_context()
        unverified.check_hostname = False
        unverified.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=unverified))
        try:
            with opener.open(url, timeout=15) as response:
                body = response.read(len(token) + 64).decode("utf-8", "replace").strip()
            if body == token:
                return True, "ok"
            return False, "something else answered on port 80 (the file came back with different content)"
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code} from the server that answered"
        except Exception as exc:  # noqa: BLE001 - any failure here is advisory
            return False, str(exc)
        finally:
            try:
                probe.unlink()
            except OSError:
                pass

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
