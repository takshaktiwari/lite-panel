"""Regression test for the per-site nginx vhost template.

A site with PHP assigned failed to create on the live server with:

    "try_files" directive is duplicate in /etc/nginx/snippets/fastcgi-php.conf:5

The vhost's PHP location block had its own `try_files $uri =404;` *and*
`include snippets/fastcgi-php.conf;` -- that stock Debian/Ubuntu snippet
already sets its own try_files against $fastcgi_script_name after splitting
PATH_INFO, and nginx rejects a second one in the same location outright.
This can't be caught by running real nginx locally (not installed on
macOS, and not part of this project's test environment even on Linux), so
the regression test is a string-level check on the rendered template: no
number of correct-looking individual lines matters if two of them collide
the way nginx defines "duplicate directive in the same context."
"""

from types import SimpleNamespace

from app.services import renderer


def _render_vhost(*, php: bool) -> str:
    site = SimpleNamespace(
        name="blog",
        domain="blog.example.com",
        webroot="/var/www/blog",
        upload_limit_mb=128,
        redirect_www=True,
        ssl_enabled=False,
    )
    return renderer.render(
        "vhost.conf.j2",
        {
            "site": site,
            "server_names": ["blog.example.com", "www.blog.example.com"],
            "fpm_socket": "/run/php/lite-panel-blog.sock" if php else None,
            "acme_root": "/var/www/lite-panel-acme",
            "www_redirect_only": True,
            "www_https_redirect": False,
        },
    )


def test_php_location_does_not_duplicate_try_files_from_the_fastcgi_snippet():
    output = _render_vhost(php=True)

    assert "include snippets/fastcgi-php.conf;" in output

    # The php location block is the only place the fastcgi include appears;
    # isolate it and confirm no try_files *directive* was also added there --
    # strip comment lines first (nginx ignores them too), since an
    # explanatory comment mentioning why there's no try_files here is exactly
    # the kind of thing that would otherwise make this assertion self-defeat.
    php_block_start = output.index("location ~ \\.php$")
    php_block_end = output.index("}", php_block_start)
    php_block = output[php_block_start:php_block_end]
    directive_lines = [
        line for line in php_block.splitlines() if not line.strip().startswith("#")
    ]

    assert "try_files" not in "\n".join(directive_lines), (
        "the PHP location block must not set its own try_files -- "
        "snippets/fastcgi-php.conf already does, and nginx rejects two "
        "try_files directives in the same location as a duplicate"
    )


def test_static_asset_location_keeps_its_own_try_files():
    """The duplicate is specific to the fastcgi include -- the static-asset
    location has no such include and should keep its own try_files."""
    output = _render_vhost(php=True)

    static_block_start = output.index("expires max")
    surrounding = output[static_block_start - 200 : static_block_start]
    assert "try_files $uri =404;" in surrounding


def test_vhost_without_php_has_no_fastcgi_include_at_all():
    output = _render_vhost(php=False)
    assert "fastcgi-php.conf" not in output
    assert "return 404;" in output
