"""Pre-flight checks run before anything is asked of Let's Encrypt.

Enabling HTTPS for a domain with no DNS record used to spend a minute in
certbot and come back with a wall of ACME output whose one relevant line
(NXDOMAIN) was buried -- and each of those attempts consumed one of the five
failed validations Let's Encrypt allows per hostname per hour. The checks
under test here catch that case locally, in about a second, and say what to
do about it.

The split that matters: a name that does not resolve is a hard stop (nothing
can make that succeed), while a challenge file that cannot be fetched back is
only a warning, since the fetch happens from this box and a server that
cannot reach its own public hostname is not proof the CA cannot either.
"""

from unittest.mock import MagicMock

import pytest

from app.providers.certbot import CertbotProvider
from app.validators import ValidationError


class _FakeCtx:
    def __init__(self):
        self.lines = []
        self.commands = []
        self.returncode = 0

    def log(self, line):
        self.lines.append(str(line))

    def run(self, args, **_kwargs):
        self.commands.append(list(args))
        return self.returncode

    @property
    def text(self):
        return "\n".join(self.lines)


@pytest.fixture
def provider(monkeypatch, tmp_path):
    """A provider wired to a scratch ACME root, with certbot reported as
    installed so tests exercise the checks rather than the install guard."""
    from app.providers import certbot as certbot_module

    monkeypatch.setattr(certbot_module, "ACME_ROOT", tmp_path / "acme")
    instance = CertbotProvider()
    monkeypatch.setattr(instance, "is_installed", lambda: True)
    return instance


def _no_network(monkeypatch, provider, *, ips, reachable=(True, "ok")):
    monkeypatch.setattr(provider, "_resolved_ips", lambda hostname: set(ips))
    monkeypatch.setattr(provider, "_resolves", lambda hostname: False)
    monkeypatch.setattr(provider, "_challenge_reachable", lambda domain: reachable)

    from app.services import system

    monkeypatch.setattr(system, "public_ip", lambda: "203.0.113.10")


def test_a_domain_with_no_dns_record_never_reaches_certbot(provider, monkeypatch):
    """The exact failure this exists to prevent: certbot is not run at all."""
    _no_network(monkeypatch, provider, ips=set())
    ctx = _FakeCtx()

    with pytest.raises(ValidationError) as excinfo:
        provider.issue(ctx, "b-paris.example.com")

    assert ctx.commands == [], "certbot must not run for a domain that cannot resolve"
    message = str(excinfo.value)
    assert "no DNS record" in message
    assert "203.0.113.10" in message, "the message should say which IP to point the record at"
    assert "rate limit" in message, "operators need to know a blind retry costs them a slot"


def test_a_resolving_domain_proceeds_to_certbot(provider, monkeypatch):
    _no_network(monkeypatch, provider, ips={"203.0.113.10"})
    ctx = _FakeCtx()

    provider.issue(ctx, "ok.example.com")

    assert len(ctx.commands) == 1
    assert ctx.commands[0][:2] == ["certbot", "certonly"]


def test_resolving_elsewhere_is_a_note_not_a_refusal(provider, monkeypatch):
    """A proxied domain (Cloudflare) resolves to the proxy, not to us. That is
    a normal, working setup and must not be treated as an error."""
    _no_network(monkeypatch, provider, ips={"104.21.73.154"})
    ctx = _FakeCtx()

    provider.issue(ctx, "proxied.example.com")

    assert len(ctx.commands) == 1, "a proxied domain must still be attempted"
    assert "does not resolve to this server" in ctx.text
    assert "Cloudflare" in ctx.text


def test_an_unreachable_challenge_warns_but_still_tries(provider, monkeypatch):
    """The self-fetch leaves and re-enters this box's own network. It failing
    is worth reporting, but is not evidence the CA would fail too."""
    _no_network(
        monkeypatch, provider, ips={"203.0.113.10"}, reachable=(False, "timed out")
    )
    ctx = _FakeCtx()

    provider.issue(ctx, "maybe.example.com")

    assert len(ctx.commands) == 1, "a failed self-check must not block issuance"
    assert "warning" in ctx.text
    assert "timed out" in ctx.text


def test_a_failed_challenge_fetch_behind_a_proxy_is_not_reported_as_a_warning(
    provider, monkeypatch
):
    """Observed on a real Cloudflare-proxied domain: the probe comes back 403
    while that domain's certificate issues perfectly well. Behind a proxy the
    check proves nothing, so it must not put a warning in the log of a site
    that is working -- that is just the next "why is this broken" report."""
    _no_network(
        monkeypatch,
        provider,
        ips={"104.21.73.154"},  # a proxy address, not this server
        reachable=(False, "HTTP 403 from the server that answered"),
    )
    ctx = _FakeCtx()

    provider.issue(ctx, "proxied.example.com")

    assert len(ctx.commands) == 1
    assert "warning" not in ctx.text, ctx.text
    assert "normal for a proxied domain" in ctx.text


def test_certbot_failure_no_longer_blames_dns(provider, monkeypatch):
    """DNS was already verified, so sending the operator to check DNS would
    point them at the one thing known to be fine."""
    _no_network(monkeypatch, provider, ips={"203.0.113.10"})
    ctx = _FakeCtx()
    ctx.returncode = 1

    with pytest.raises(RuntimeError) as excinfo:
        provider.issue(ctx, "fails.example.com")

    message = str(excinfo.value)
    assert "does resolve" in message
    assert "port 80" in message


def test_challenge_probe_round_trips_through_a_stubbed_fetch(provider, monkeypatch, tmp_path):
    """The real _challenge_reachable: it must write the token where nginx
    serves it, ask for that exact URL, and clean the file up afterward."""
    from app.providers import certbot as certbot_module

    requested = {}

    class _Response:
        def __init__(self, body):
            self._body = body

        def read(self, _size):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def fake_build_opener(*_handlers):
        opener = MagicMock()

        def _open(url, timeout=None):
            requested["url"] = url
            # Serve back whatever is actually on disk at the requested path.
            token = url.rsplit("/", 1)[-1]
            path = certbot_module.ACME_ROOT / certbot_module.CHALLENGE_SUBPATH / token
            return _Response(path.read_bytes())

        opener.open = _open
        return opener

    monkeypatch.setattr(certbot_module.urllib.request, "build_opener", fake_build_opener)

    ok, detail = provider._challenge_reachable("example.com")

    assert ok, detail
    assert requested["url"].startswith("http://example.com/.well-known/acme-challenge/")

    directory = certbot_module.ACME_ROOT / certbot_module.CHALLENGE_SUBPATH
    assert list(directory.iterdir()) == [], "the probe file must not be left behind"


def test_challenge_probe_reports_wrong_content_rather_than_claiming_success(
    provider, monkeypatch
):
    """A catch-all vhost or a parked page answering on port 80 returns 200 with
    the wrong body -- that is a failure, not a pass."""
    from app.providers import certbot as certbot_module

    class _Response:
        def read(self, _size):
            return b"<html>some parked page</html>"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def fake_build_opener(*_handlers):
        opener = MagicMock()
        opener.open = lambda url, timeout=None: _Response()
        return opener

    monkeypatch.setattr(certbot_module.urllib.request, "build_opener", fake_build_opener)

    ok, detail = provider._challenge_reachable("example.com")

    assert not ok
    assert "different content" in detail
