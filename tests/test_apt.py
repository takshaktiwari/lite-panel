"""Tests for apt output parsing.

These are pure string-parsing functions, tested against captured/fixture
output rather than a real apt-cache, so they run on any machine. What they
protect: the multi-PHP model depends entirely on this parsing being right --
if it silently returns the wrong versions, the panel would offer to install
something that doesn't exist, or hide one that does.
"""

from app.services import apt


def test_parse_php_versions_only_counts_versions_with_fpm():
    """The panel serves through FPM; a version without that package is
    useless to it even if other php8.x-* packages exist."""
    output = "\n".join(
        [
            "php8.1-fpm",
            "php8.1-cli",
            "php8.2-fpm",
            "php8.2-mbstring",
            "php8.3-cli",  # no php8.3-fpm: must not count as available
            "php-common",
            "phpunit",
        ]
    )
    assert apt.parse_php_versions(output) == ["8.1", "8.2"]


def test_parse_php_versions_sorts_numerically_not_lexically():
    """8.10 > 8.9 numerically but would sort before it as strings."""
    output = "php8.9-fpm\nphp8.10-fpm\nphp8.2-fpm"
    assert apt.parse_php_versions(output) == ["8.2", "8.9", "8.10"]


def test_parse_php_versions_empty_output():
    assert apt.parse_php_versions("") == []


def test_parse_php_extensions_excludes_core_packages():
    output = "\n".join(
        [
            "php8.2-fpm",
            "php8.2-cli",
            "php8.2-common",
            "php8.2-readline",
            "php8.2-opcache",
            "php8.2-mbstring",
            "php8.2-curl",
            "php8.2-gd",
        ]
    )
    extensions = apt.parse_php_extensions(output, "8.2")
    assert extensions == ["curl", "gd", "mbstring"]
    for core in apt.PHP_CORE_SUFFIXES:
        assert core not in extensions


def test_parse_php_extensions_only_matches_the_requested_version():
    output = "php8.1-mbstring\nphp8.2-mbstring\nphp8.2-gd"
    assert apt.parse_php_extensions(output, "8.2") == ["gd", "mbstring"]
    assert apt.parse_php_extensions(output, "8.1") == ["mbstring"]


def test_parse_php_extensions_handles_hyphenated_names():
    """Extension names like xmlrpc or imagick may contain their own hyphens
    beyond the php{version}- prefix."""
    output = "php8.2-fpm\nphp8.2-xml\nphp8.2-imagick"
    assert "xml" in apt.parse_php_extensions(output, "8.2")
    assert "imagick" in apt.parse_php_extensions(output, "8.2")


def test_parse_dpkg_query_only_returns_installed_packages():
    output = "\n".join(
        [
            "php8.2-fpm|install ok installed|8.2.10-1",
            "php8.2-old|deinstall ok config-files|8.2.1-1",
            "php8.3-fpm|unknown ok not-installed|",
            "malformed line without pipes",
        ]
    )
    result = apt.parse_dpkg_query(output)
    assert result == {"php8.2-fpm": "8.2.10-1"}


def test_parse_dpkg_query_empty_output():
    assert apt.parse_dpkg_query("") == {}


def test_sort_versions():
    assert apt.sort_versions(["8.3", "7.4", "8.10", "8.1"]) == ["7.4", "8.1", "8.3", "8.10"]


# --------------------------------------------------------------------------
# has_candidate parsing
#
# This is the regression test for a real bug: PHP 8.2 was offered and then
# failed to install on an Ubuntu release the ondrej/php PPA doesn't cover yet
# (26.04 "resolute") and that the OS's own repos don't ship. `apt-cache
# pkgnames` alone wasn't the culprit in the end (it correctly omitted 8.2),
# but a package name can still be *referenced* by other packages' dependency
# metadata without a real install candidate ever existing -- this is the
# check that catches that case regardless of how a bad name reaches it.
# --------------------------------------------------------------------------


def test_has_candidate_true_when_a_real_candidate_exists():
    output = (
        "php8.5-fpm:\n"
        "  Installed: (none)\n"
        "  Candidate: 8.5.4-0ubuntu1.2\n"
        "  Version table:\n"
        "     8.5.4-0ubuntu1.2 500\n"
        "        500 http://archive.ubuntu.com/ubuntu resolute/universe amd64 Packages\n"
    )
    assert apt.parse_apt_cache_policy_has_candidate(output) is True


def test_has_candidate_false_when_candidate_is_none():
    """The exact shape apt-cache policy prints for a name it has heard of
    (e.g. via another package's dependency list) but cannot actually install."""
    output = "php8.2-fpm:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n"
    assert apt.parse_apt_cache_policy_has_candidate(output) is False


def test_has_candidate_false_for_completely_unknown_package():
    """apt-cache policy on a name it has never heard of prints nothing at
    all -- no Candidate line to find."""
    assert apt.parse_apt_cache_policy_has_candidate("") is False


def test_has_candidate_false_for_installed_package_with_no_candidate():
    """An installed package whose source was since removed: Installed is set
    but Candidate reverts to none. Still not something we can (re)install."""
    output = "some-pkg:\n  Installed: 1.0-1\n  Candidate: (none)\n  Version table:\n"
    assert apt.parse_apt_cache_policy_has_candidate(output) is False
