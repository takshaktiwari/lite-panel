"""Regression tests for the PHP provider's version-availability logic.

The bug this file exists to pin down: PHP 8.2 was offered in the setup
wizard and Stack page, then failed with a wall of raw apt-get errors, on a
machine (Ubuntu 26.04 "resolute") where it does not exist -- the ondrej/php
PPA has no build for that release, and the OS's own repos ship only PHP 8.5.
The cause was a static "known PPA versions" list silently merged into
available_versions() whenever a single (fragile, process-cached) network
probe returned True. That merge is gone; these tests assert it stays gone.
"""

from unittest.mock import patch

from app.providers.php import PhpProvider


def _provider():
    return PhpProvider()


# --------------------------------------------------------------------------
# The regression itself
# --------------------------------------------------------------------------


def test_available_versions_never_offers_a_version_apt_does_not_have():
    """Even if PHP is generally known to reach these versions somewhere, if
    apt-cache pkgnames on *this* machine only knows about 8.5, that's all
    that may be offered -- no static wishlist merged in."""
    provider = _provider()
    with patch("app.services.apt.apt_available", return_value=True), \
         patch("app.services.apt.package_names", return_value=["php8.5-fpm", "php8.5-cli"]), \
         patch("app.services.apt.has_candidate", return_value=True):
        assert provider.available_versions() == ["8.5"]


def test_available_versions_drops_names_with_no_real_candidate():
    """Defense in depth: even if a version's -fpm name shows up in pkgnames
    output, it's excluded unless apt-cache policy confirms a real candidate --
    covering the "referenced but not installable" apt phenomenon directly."""
    provider = _provider()

    def fake_has_candidate(package):
        return package == "php8.5-fpm"  # 8.2 looks present but has no candidate

    with patch("app.services.apt.apt_available", return_value=True), \
         patch(
             "app.services.apt.package_names",
             return_value=["php8.2-fpm", "php8.5-fpm"],
         ), \
         patch("app.services.apt.has_candidate", side_effect=fake_has_candidate):
        assert provider.available_versions() == ["8.5"]


def test_available_versions_is_empty_when_apt_has_nothing_and_not_dev_mode():
    provider = _provider()
    with patch("app.services.apt.apt_available", return_value=True), \
         patch("app.services.apt.package_names", return_value=[]):
        assert provider.available_versions() == []


def test_available_versions_never_consults_a_static_ppa_wishlist():
    """The old bug's exact shape: is_php_ppa_supported() returning True must
    no longer be able to inject versions that were never seen from apt."""
    provider = _provider()
    with patch("app.services.apt.apt_available", return_value=True), \
         patch("app.services.apt.package_names", return_value=["php8.5-fpm"]), \
         patch("app.services.apt.has_candidate", return_value=True), \
         patch("app.services.apt.is_php_ppa_supported", return_value=True), \
         patch("app.services.apt._ppa_present", return_value=False):
        assert provider.available_versions() == ["8.5"]


# --------------------------------------------------------------------------
# install() still refuses cleanly if something upstream ever regresses
# --------------------------------------------------------------------------


def test_install_refuses_a_version_not_in_available_versions():
    from app.validators import ValidationError

    provider = _provider()
    ctx = _FakeCtx()

    with patch.object(provider, "is_version_installed", return_value=False), \
         patch("app.services.apt.ensure_php_repository"), \
         patch.object(provider, "available_versions", return_value=["8.5"]):
        try:
            provider.install(ctx, "8.2")
            assert False, "expected a ValidationError"
        except ValidationError as exc:
            assert "8.2" in str(exc)
            assert "8.5" in str(exc)


# --------------------------------------------------------------------------
# repository_status()
# --------------------------------------------------------------------------


def test_repository_status_reports_unsupported_release_honestly():
    provider = _provider()
    with patch("app.services.apt._ppa_present", return_value=False), \
         patch("app.services.apt._sury_present", return_value=False), \
         patch("app.services.apt.is_php_ppa_supported", return_value=False), \
         patch("app.services.system.os_release_id", return_value="ubuntu"):
        status = provider.repository_status()
        assert status["present"] is False
        assert status["supported"] is False


def test_repository_status_when_already_present_skips_the_probe():
    """Once the repo is configured, there's no need to ask "could it be" --
    it already is, and the live network probe is skipped entirely."""
    provider = _provider()
    with patch("app.services.apt._ppa_present", return_value=True), \
         patch("app.services.apt.is_php_ppa_supported") as probe:
        status = provider.repository_status()
        assert status["present"] is True
        assert status["supported"] is True
        probe.assert_not_called()


class _FakeCtx:
    def log(self, *_args, **_kwargs):
        pass
