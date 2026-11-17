"""Tests for splitting the single "folder" field on the create-site form
into (name, subfolder).

The form shows one input after a fixed /var/www/ prefix: the first path
segment becomes the site's own folder (and the name its system user is
derived from), and anything typed after a "/" is an optional extra nesting
for the webroot -- e.g. "blog/public" serves from /var/www/blog/public while
the site itself still lives at /var/www/blog.
"""

import pytest

from app.routers.sites import _clean_subfolder, _split_folder
from app.validators import ValidationError


def test_blank_folder_falls_back_to_a_name_derived_from_the_domain():
    name, subfolder = _split_folder("", "blog.example.com")
    assert name == "blog-example_com"
    assert subfolder == ""


def test_a_single_segment_becomes_the_name_with_no_subfolder():
    name, subfolder = _split_folder("myblog", "example.com")
    assert name == "myblog"
    assert subfolder == ""


def test_a_second_segment_becomes_an_optional_subfolder():
    """The "add public if you want" case: nobody is forced into a subfolder,
    but typing one nests the webroot under it."""
    name, subfolder = _split_folder("myblog/public", "example.com")
    assert name == "myblog"
    assert subfolder == "public"


def test_more_than_two_segments_nest_further():
    name, subfolder = _split_folder("app/backend/public", "example.com")
    assert name == "app"
    assert subfolder == "backend/public"


def test_leading_and_trailing_slashes_are_ignored():
    name, subfolder = _split_folder("/myblog/public/", "example.com")
    assert name == "myblog"
    assert subfolder == "public"


def test_repeated_slashes_are_collapsed():
    name, subfolder = _split_folder("myblog//public", "example.com")
    assert name == "myblog"
    assert subfolder == "public"


def test_whitespace_is_stripped():
    name, subfolder = _split_folder("  myblog  ", "example.com")
    assert name == "myblog"
    assert subfolder == ""


def test_malformed_first_segment_is_rejected():
    with pytest.raises(ValidationError):
        _split_folder("-bad-name-", "example.com")


def test_traversal_in_the_subfolder_is_rejected():
    with pytest.raises(ValidationError):
        _split_folder("myblog/../../etc", "example.com")


def test_traversal_as_the_whole_subfolder_is_rejected():
    with pytest.raises(ValidationError):
        _split_folder("myblog/..", "example.com")


def test_clean_subfolder_blank_is_the_site_root():
    assert _clean_subfolder("") == ""
    assert _clean_subfolder("   ") == ""


def test_clean_subfolder_normalises_slashes():
    assert _clean_subfolder("/public/") == "public"
    assert _clean_subfolder("current//public") == "current/public"


def test_clean_subfolder_rejects_traversal():
    with pytest.raises(ValidationError):
        _clean_subfolder("../etc")
    with pytest.raises(ValidationError):
        _clean_subfolder("public/../../etc")


def test_derived_name_keeps_the_tld_and_trims_the_start_when_too_long():
    from app.routers.sites import _name_from_domain

    assert _name_from_domain("ai.eoschambersoflaw.com") == "ai-eoschambersoflaw_com"
    assert _name_from_domain("my.thehelm.co.in") == "my-thehelm-co_in"
    assert _name_from_domain("example.com") != _name_from_domain("example.in")
    long = _name_from_domain("a-very-long-subdomain-name.example-company.com")
    assert len(long) <= 27 and long.endswith("_com")
    # the default must always give a valid Linux user (site_<name>, max 32)
    from app.validators import site_username

    assert len(site_username(long)) <= 32
    assert _name_from_domain("localhost") == "localhost"
