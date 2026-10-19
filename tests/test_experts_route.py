"""The expert-answer audit page, served from this server rather than downloaded.

The page is a generated snapshot. These tests pin the three things that would
let it rot silently: the route existing at all, the bytes served matching the
file on disk, and the path staying under /audit.

The path matters. The EDITO ingress forwards a fixed list of named paths to this
server and sends everything else to the web terminal, so a route at /experts
answers a login prompt from outside the cluster while working perfectly inside
it. /audit/experts inherits a prefix the ingress already forwards.
"""
import pytest
from starlette.testclient import TestClient

from surimi_data_mcp.paths import APP_ROOT
from surimi_data_mcp.web import experts_routes

# The evaluation pages are not distributed with this repository; a deployment adds them to
# static/experts/. The tests that read a page run only where pages are present.
_VERSIONS = experts_routes.available_versions()
STATIC = experts_routes.page_path(_VERSIONS[-1]) if _VERSIONS else None
needs_pages = pytest.mark.skipif(not _VERSIONS, reason="no evaluation pages in static/experts/")


def _client():
    from starlette.applications import Starlette
    return TestClient(Starlette(routes=experts_routes.make_experts_routes()))


@needs_pages
def test_the_page_is_served():
    r = _client().get("/audit/experts")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


@needs_pages
def test_the_served_bytes_are_the_file_on_disk():
    """A drifting copy would show a reader stale findings while looking live."""
    assert _client().get("/audit/experts").text == STATIC.read_text(encoding="utf-8")


@needs_pages
def test_the_page_is_shipped():
    assert STATIC.exists(), "no versioned audit page in static/experts/"
    assert STATIC.stat().st_size > 100_000, "page looks truncated"


def test_the_route_lives_under_the_audit_prefix():
    """Outside the cluster, a path the ingress does not name reaches the web
    terminal instead of this server. /experts did exactly that."""
    paths = [r.path for r in experts_routes.make_experts_routes()]
    assert all(p.startswith("/audit/") for p in paths), paths


@needs_pages
def test_the_page_names_the_versions_it_describes():
    """It is a snapshot. A reader must be able to see which pair produced it."""
    html = STATIC.read_text(encoding="utf-8")
    assert "data-mcp" in html and "model-mcp" in html


def test_the_dockerfile_ships_the_static_directory():
    """audit.html is served the same way; if this COPY ever goes, both break."""
    df = (APP_ROOT / "Dockerfile").read_text()
    assert "COPY static/ static/" in df
