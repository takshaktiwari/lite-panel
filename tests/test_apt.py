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
