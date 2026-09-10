"""Pure path-computation tests for site creation.

These cover root_dir_for/webroot_for/socket_for directly -- no filesystem or
subprocess calls, so they run anywhere. The rest of services/sites.py (real
useradd, chown, nginx reload) is only meaningfully verifiable on a real
server, per the project's usual split between what's unit-testable and what
needs the live box.
"""

from pathlib import Path

from app.services import sites as sites_service


def test_sites_root_matches_settings():
    from app.config import get_settings

    assert sites_service.sites_root() == get_settings().sites_root
    assert sites_service.sites_root() == Path("/var/www")


def test_root_dir_uses_the_plain_site_name_not_the_prefixed_username():
    """The directory a person browses to (FTP, file manager) should read
    /var/www/blog -- not /var/www/site_blog, which is the *system user's*
    name, a different and intentionally separate namespace."""
    assert sites_service.root_dir_for("blog") == Path("/var/www/blog")


def test_webroot_defaults_to_the_root_dir_itself():
    """No subfolder is the default -- a "public" wrapper is opt-in (typed
    explicitly as part of the folder field), not forced on every site."""
    assert sites_service.webroot_for("blog") == sites_service.root_dir_for("blog")
    assert sites_service.webroot_for("blog") == Path("/var/www/blog")


def test_webroot_appends_a_subfolder_when_given():
    assert sites_service.webroot_for("blog", "public") == Path("/var/www/blog/public")
    assert sites_service.webroot_for("blog", "web") == Path("/var/www/blog/web")


def test_socket_path_is_namespaced_by_site_name():
    assert sites_service.socket_for("blog") == Path("/run/php/lite-panel-blog.sock")


def test_root_dir_rejects_a_malformed_name():
    from app.validators import ValidationError

    import pytest

    with pytest.raises(ValidationError):
        sites_service.root_dir_for("../etc")


def test_relative_webroot_is_blank_when_it_equals_root_dir():
    from app.models import Site

    site = Site(root_dir="/var/www/blog", webroot="/var/www/blog")
    assert sites_service.relative_webroot(site) == ""


def test_relative_webroot_reports_the_subfolder():
    from app.models import Site

    site = Site(root_dir="/var/www/blog", webroot="/var/www/blog/public")
    assert sites_service.relative_webroot(site) == "public"
