"""The built-in documentation: access, completeness and link integrity."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.routers.docs import GROUPS, PAGES

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "panel" / "app" / "templates"
DOC_DIR = TEMPLATE_DIR / "docs"
SLUGS = {page["slug"] for page in PAGES}


@pytest.fixture
def client(db, admin):
    with TestClient(create_app(), follow_redirects=False) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client):
    response = client.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
    assert response.status_code == 303
    return client


def test_docs_require_login(client):
    assert client.get("/docs").status_code == 303
    assert client.get("/docs/sites").status_code == 303


def test_docs_home_lists_every_page(signed_in):
    page = signed_in.get("/docs")
    assert page.status_code == 200
    for item in PAGES:
        assert f'href="/docs/{item["slug"]}"' in page.text


@pytest.mark.parametrize("slug", sorted(SLUGS))
def test_every_registered_page_renders_with_its_title(signed_in, slug):
    page = signed_in.get(f"/docs/{slug}")
    assert page.status_code == 200
    title = next(p["title"] for p in PAGES if p["slug"] == slug)
    assert f"<h1>{title.replace('&', '&amp;')}</h1>" in page.text
    assert 'class="docs-nav"' in page.text


def test_unknown_page_is_a_404(signed_in):
    assert signed_in.get("/docs/does-not-exist").status_code == 404
    assert signed_in.get("/docs/..%2Fetc").status_code in (404, 400)


def test_every_page_has_a_template_and_every_template_is_registered():
    on_disk = {p.stem for p in DOC_DIR.glob("*.html") if not p.name.startswith("_")} - {"index"}
    assert on_disk == SLUGS


def test_page_slugs_are_unique():
    slugs = [page["slug"] for page in PAGES]
    assert len(slugs) == len(set(slugs))


def test_previous_and_next_links_walk_the_whole_list(signed_in):
    first, last = PAGES[0]["slug"], PAGES[-1]["slug"]
    assert "Previous" not in signed_in.get(f"/docs/{first}").text
    assert "Next" in signed_in.get(f"/docs/{first}").text
    assert "Next" not in signed_in.get(f"/docs/{last}").text
    assert "Previous" in signed_in.get(f"/docs/{last}").text


def test_current_page_is_highlighted_in_the_menu(signed_in):
    page = signed_in.get("/docs/firewall").text
    assert re.search(r'href="/docs/firewall" class="active"', page)


def test_sidebar_has_a_documentation_link(signed_in):
    assert 'href="/docs"' in signed_in.get("/sites").text


def test_internal_doc_links_and_anchors_resolve():
    """Every /docs/<slug>#anchor written in any template points at a real
    page, and a real id on that page."""
    ids = {}
    for path in DOC_DIR.glob("*.html"):
        ids[path.stem] = set(re.findall(r'id="([^"]+)"', path.read_text()))

    link = re.compile(r'href="/docs/([a-z0-9-]+)(?:#([a-z0-9-]+))?"')
    problems = []
    for path in TEMPLATE_DIR.rglob("*.html"):
        for slug, anchor in link.findall(path.read_text()):
            if slug not in SLUGS:
                problems.append(f"{path.name}: unknown page /docs/{slug}")
            elif anchor and anchor not in ids.get(slug, set()):
                problems.append(f"{path.name}: /docs/{slug}#{anchor} has no such id")
    assert not problems, "\n".join(problems)


def test_groups_cover_all_pages():
    assert sum(len(items) for _group, items in GROUPS) == len(PAGES)
