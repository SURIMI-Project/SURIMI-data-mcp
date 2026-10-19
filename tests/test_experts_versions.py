"""Every published audit keeps its own permanent URL.

The page is a snapshot of one (data-mcp, model-mcp) pair. Serving only the
latest at a fixed path means an older finding cited in an email or a paper
silently changes underneath the citation. Each version therefore gets its own
path, and /audit/experts redirects to the newest.
"""
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from surimi_data_mcp.web import experts_routes as er


def _client():
    return TestClient(Starlette(routes=er.make_experts_routes()))


# The evaluation pages are not distributed with this repository; a deployment adds them to
# static/experts/. The tests that read a page run only where pages are present.
needs_pages = pytest.mark.skipif(not er.available_versions(),
                                 reason="no evaluation pages in static/experts/")


def test_without_pages_the_routes_answer_404_not_a_server_error(tmp_path, monkeypatch):
    monkeypatch.setattr(er, "_DIR", tmp_path)
    c = _client()
    assert c.get("/audit/experts").status_code == 404
    assert c.get("/audit/experts/index.json").status_code == 404
    assert c.get("/audit/experts/v_dm1.2.43_mm1.2.11").status_code == 404


@needs_pages
def test_versions_are_discovered_from_disk():
    """No hand-maintained index to fall out of step with the directory."""
    vs = er.available_versions()
    assert vs, "no versioned pages found in static/experts/"
    assert all(v.startswith("v_") for v in vs), vs


@needs_pages
def test_the_bare_path_serves_the_newest():
    r = _client().get("/audit/experts")
    assert r.status_code == 200
    assert r.text == er.page_path(er.latest_version()).read_text(encoding="utf-8")


@needs_pages
def test_an_older_version_says_it_is_superseded_and_links_the_current_page():
    older = [v for v in er.available_versions() if v != er.latest_version()]
    assert older
    c = _client()
    for v in older:
        html = c.get(f"/audit/experts/{v}").text
        assert 'id="superseded"' in html, v
        assert 'href="/audit/experts"' in html, v


@needs_pages
def test_the_newest_version_has_no_superseded_banner():
    assert 'id="superseded"' not in _client().get("/audit/experts").text
    latest = _client().get(f"/audit/experts/{er.latest_version()}").text
    assert 'id="superseded"' not in latest


@needs_pages
def test_each_version_has_its_own_permanent_path():
    c = _client()
    for v in er.available_versions():
        r = c.get(f"/audit/experts/{v}")
        assert r.status_code == 200, v
        assert r.headers["content-type"].startswith("text/html")


def test_an_unknown_version_is_404_not_a_traversal():
    c = _client()
    for bad in ["v_nope", "..", "%2e%2e", "etc"]:
        assert c.get(f"/audit/experts/{bad}").status_code == 404, bad


def test_a_traversal_attempt_cannot_escape_the_directory():
    """The version is matched against what is on disk, never joined blindly."""
    with pytest.raises(KeyError):
        er.page_path("../../../etc/passwd")


@needs_pages
def test_the_index_lists_only_the_current_page_and_names_the_rest_superseded():
    r = _client().get("/audit/experts/index.json")
    assert r.status_code == 200
    body = r.json()
    assert body["latest"] == er.latest_version()
    assert body["versions"] == [er.latest_version()]
    assert set(body["superseded"]) == set(er.available_versions()) - {er.latest_version()}


def test_versions_order_numerically_not_lexicographically():
    """1.2.13 is newer than 1.2.8, and a string sort says the opposite.

    `sorted()` on the stem compares "1" against "8" at the first differing
    character, so `v_dm1.2.8_mm1.2.4` sorts after `v_dm1.2.13_mm1.2.5` and
    `latest_version()` keeps returning the older page. Every release after
    1.2.9 would have published a page the bare path never serves, with the
    directory listing and the release checklist both reporting success.
    """
    names = [
        "v_dm1.1.2_mm1.2.2",
        "v_dm1.1.2_mm1.2.2_r7",
        "v_dm1.2.6_mm1.2.3",
        "v_dm1.2.8_mm1.2.4",
        "v_dm1.2.13_mm1.2.5",
    ]
    assert sorted(names, key=er.version_key)[-1] == "v_dm1.2.13_mm1.2.5"
    assert er.version_key("v_dm1.2.13_mm1.2.5") > er.version_key("v_dm1.2.8_mm1.2.4")
    assert er.version_key("v_dm1.1.2_mm1.2.2_r7") > er.version_key("v_dm1.1.2_mm1.2.2")


def test_an_unparseable_name_sorts_first_and_does_not_raise():
    """A stray file must not take the latest slot, nor crash the route."""
    assert er.version_key("v_whatever") < er.version_key("v_dm1.1.2_mm1.2.2")
