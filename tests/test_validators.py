"""Tests for the validation choke point.

These run on a workstation -- no server needed.  They exist because every
function here stands between a browser form and a root-privileged operation.
"""

import os
from pathlib import Path

import pytest

from app import validators as v
from app.validators import ValidationError


# --------------------------------------------------------------------------
# Domains
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("example.com", "example.com"),
        ("EXAMPLE.COM", "example.com"),
        ("  example.com  ", "example.com"),
        ("example.com.", "example.com"),
        ("sub.domain.example.co.uk", "sub.domain.example.co.uk"),
        ("my-site1.example.com", "my-site1.example.com"),
    ],
)
def test_valid_domains(value, expected):
    assert v.validate_domain(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "localhost",                      # no TLD
        "-example.com",                   # leading hyphen
        "example-.com",                   # trailing hyphen
        "exa mple.com",                   # space
        "example..com",                   # empty label
        "192.168.1.1",                    # numeric TLD
        "example.com/../../etc/passwd",   # path traversal
        "example.com/evil",               # separator
        "example.com\x00.evil",           # null byte
        "example.com\nserver_name evil",  # config injection via newline
        "exa_mple.com",                   # underscore
        "a" * 250 + ".com",               # too long
    ],
)
def test_rejected_domains(value):
    with pytest.raises(ValidationError):
        v.validate_domain(value)


def test_domain_rejects_shell_metacharacters():
    """These would be inert in an argument list, but they'd still land in a
    filename and an nginx server_name."""
    for value in ["example.com;rm -rf /", "example.com`id`", "$(id).com", "a|b.com"]:
        with pytest.raises(ValidationError):
            v.validate_domain(value)


# --------------------------------------------------------------------------
# Site names and system users
# --------------------------------------------------------------------------


def test_site_name_normalises_case():
    assert v.validate_site_name("MySite") == "mysite"


@pytest.mark.parametrize("value", ["", "-site", "site-", "si te", "a" * 33, "site/../x"])
def test_rejected_site_names(value):
    with pytest.raises(ValidationError):
        v.validate_site_name(value)


def test_site_username_is_namespaced():
    """Site accounts live behind a prefix so they can never collide with a
    system account, whatever the site is called."""
    assert v.site_username("blog") == "site_blog"
    assert v.site_username("my-blog") == "site_my_blog"


@pytest.mark.parametrize("value", ["root", "www-data", "mysql", "nobody", "ubuntu"])
def test_reserved_usernames_rejected(value):
    with pytest.raises(ValidationError):
        v.validate_system_username(value)


def test_panel_admin_names_are_a_separate_namespace():
    """A panel login is an application account, never a Linux one, so the
    system reserved list must not apply to it."""
    assert v.validate_admin_username("admin") == "admin"
    assert v.validate_admin_username("Takshak") == "takshak"

    # ...but it is still refused where a real system account is created.
    with pytest.raises(ValidationError):
        v.validate_system_username("admin")


@pytest.mark.parametrize("value", ["", "-admin", "admin-", "ad min", "a" * 33, "admin/x"])
def test_rejected_admin_usernames(value):
    with pytest.raises(ValidationError):
        v.validate_admin_username(value)


def test_site_named_root_is_still_safe():
    """A site called 'root' must not produce the root account."""
    assert v.site_username("root") == "site_root"


# --------------------------------------------------------------------------
# Database identifiers
# --------------------------------------------------------------------------


def test_valid_db_identifiers():
    assert v.validate_db_identifier("app_db") == "app_db"
    assert v.validate_db_identifier("_private") == "_private"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "1abc",                            # leading digit
        "app-db",                          # hyphen
        "app db",                          # space
        "app`db",                          # backtick: the quoting escape
        "app'; DROP DATABASE x; --",       # classic injection
        "a" * 65,
        "mysql",                           # reserved schema
        "information_schema",
    ],
)
def test_rejected_db_identifiers(value):
    with pytest.raises(ValidationError):
        v.validate_db_identifier(value)


def test_quote_identifier_refuses_unvalidated_backtick():
    with pytest.raises(ValidationError):
        v.quote_identifier("app`db")
    assert v.quote_identifier("app_db") == "`app_db`"


# --------------------------------------------------------------------------
# Packages and versions
# --------------------------------------------------------------------------


def test_php_versions():
    assert v.validate_php_version("8.3") == "8.3"
    for bad in ["8", "8.3.1", "php8.3", "8.x", "", "-8.3"]:
        with pytest.raises(ValidationError):
            v.validate_php_version(bad)


def test_package_name_rejects_flag_injection():
    """An extension checkbox becomes an apt argument; a value that looks like
    a flag must not survive."""
    assert v.validate_package_name("php8.3-mbstring") == "php8.3-mbstring"
    for bad in ["--force-yes", "-y", "../evil", "pkg;rm -rf /", "", "pkg name"]:
        with pytest.raises(ValidationError):
            v.validate_package_name(bad)


# --------------------------------------------------------------------------
# Path containment -- the file manager's load-bearing control
# --------------------------------------------------------------------------


def test_resolve_within_accepts_paths_inside_root(tmp_path):
    (tmp_path / "public").mkdir()
    assert v.resolve_within(tmp_path, "public") == Path(os.path.realpath(tmp_path / "public"))
    assert v.resolve_within(tmp_path, tmp_path / "public") == Path(
        os.path.realpath(tmp_path / "public")
    )


def test_resolve_within_allows_paths_that_do_not_exist_yet(tmp_path):
    """Uploads and mkdir target files that aren't there yet."""
    target = v.resolve_within(tmp_path, "public/new-file.txt")
    assert str(target).startswith(str(Path(os.path.realpath(tmp_path))))


@pytest.mark.parametrize(
    "candidate",
    [
        "../../../etc/passwd",
        "public/../../etc/passwd",
        "/etc/passwd",
        "..",
        "public/../..",
    ],
)
def test_resolve_within_blocks_traversal(tmp_path, candidate):
    (tmp_path / "public").mkdir()
    with pytest.raises(ValidationError):
        v.resolve_within(tmp_path, candidate)


def test_resolve_within_blocks_symlink_escape(tmp_path):
    """The attack a naive ``startswith`` check misses: a symlink planted
    inside the allowed tree that points out of it."""
    root = tmp_path / "site"
    root.mkdir()
    outside = tmp_path / "secrets"
    outside.mkdir()
    (outside / "key.txt").write_text("sensitive")
    (root / "escape").symlink_to(outside)

    with pytest.raises(ValidationError):
        v.resolve_within(root, "escape/key.txt")


def test_resolve_within_blocks_null_byte(tmp_path):
    with pytest.raises(ValidationError):
        v.resolve_within(tmp_path, "public\x00/../../etc/passwd")


def test_validate_filename():
    assert v.validate_filename("index.php") == "index.php"
    for bad in ["", ".", "..", "a/b", "a\\b", "x" * 256]:
        with pytest.raises(ValidationError):
            v.validate_filename(bad)
