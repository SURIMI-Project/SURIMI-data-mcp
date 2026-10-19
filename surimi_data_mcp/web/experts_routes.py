"""Serves the expert-answer audit pages, one permanent URL for each published version.

Each page presents the scored questions from eleven fisheries experts. Older pages carry a superseded
notice, and `index.json` lists them apart from the current one.

Each page is a snapshot of one pair of data-mcp and model-mcp versions. Every published page keeps
its own path, so a finding cited in an email or a paper does not change when a server is released,
and the bare path serves the newest.

    /audit/experts              the newest page
    /audit/experts/index.json   every version, machine readable
    /audit/experts/v_dm1.1.2_mm1.2.2   that exact pair, forever

The routes live under `/audit` because the EDITO ingress already forwards that prefix to this server,
so they need no chart change and no service relaunch.

To publish a page, generate it with `benchmark/tools/expert_answer_table.py` in the monorepo and add
it to `static/experts/` as a new `v_*.html` file. This module needs no edit.
"""
from __future__ import annotations

import re
from pathlib import Path

from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
from surimi_data_mcp.paths import APP_ROOT

_DIR = APP_ROOT / "static" / "experts"


_VERSION_RE = re.compile(
    r"^v_dm(?P<dm>\d+(?:\.\d+)*)_mm(?P<mm>\d+(?:\.\d+)*)(?:_r(?P<r>\d+))?$")


def version_key(stem: str) -> tuple:
    """Return a sort key that orders version stems by their numbers.

    Comparing the stems as text would put `v_dm1.2.8_mm1.2.4` after `v_dm1.2.13_mm1.2.5`. A name that
    does not parse sorts first, so a stray file never takes the latest slot and never raises.
    """
    m = _VERSION_RE.match(stem)
    if not m:
        return (0, (), (), 0, stem)
    parts = tuple(tuple(int(n) for n in m.group(g).split("."))
                  for g in ("dm", "mm"))
    return (1, parts[0], parts[1], int(m.group("r") or 1), stem)


def available_versions() -> list[str]:
    """Return the version names of the `v_*.html` files in `static/experts`, oldest first."""
    return sorted((p.stem for p in _DIR.glob("v_*.html")), key=version_key)


def latest_version() -> str:
    """Return the newest version name. Raises `FileNotFoundError` when there are no pages."""
    vs = available_versions()
    if not vs:
        raise FileNotFoundError(f"no versioned audit pages in {_DIR}")
    return vs[-1]


def page_path(version: str) -> Path:
    """Return the file for `version`.

    The name is matched against the files on disk and not joined onto the directory, so an unknown
    name, including a path traversal attempt, raises `KeyError`.
    """
    if version not in set(available_versions()):
        raise KeyError(version)
    return _DIR / f"{version}.html"


_BANNER = (
    '<div id="superseded" style="background:#fff4d6;color:#4a3500;border-bottom:2px solid #e0b100;'
    'padding:12px 16px;font:15px/1.4 system-ui,sans-serif">'
    "This is an earlier snapshot ({version}). Its labels, counts and scores have since been "
    'revised, so do not quote it. The current page is at '
    '<a href="/audit/experts">/audit/experts</a>.</div>')


def with_banner(html: str, version: str) -> str:
    """Return `html` with the superseded notice inserted after the opening `<body>` tag. The file on disk is unchanged."""
    banner = _BANNER.format(version=version)
    m = re.search(r"<body[^>]*>", html)
    return html[:m.end()] + banner + html[m.end():] if m else banner + html


def serve(version: str) -> str:
    """Return the HTML of a version, with the superseded notice unless it is the newest."""
    html = page_path(version).read_text(encoding="utf-8")
    return html if version == latest_version() else with_banner(html, version)


def _no_pages() -> HTMLResponse:
    return HTMLResponse("no evaluation pages on this server", status_code=404)


def make_experts_routes() -> list[Route]:
    """Return the three `/audit/experts` routes. Without pages in `static/experts`, each answers 404."""
    async def newest(request):
        if not available_versions():
            return _no_pages()
        return HTMLResponse(serve(latest_version()))

    async def index(request):
        if not available_versions():
            return _no_pages()
        latest = latest_version()
        return JSONResponse({"latest": latest, "versions": [latest],
                             "superseded": [v for v in available_versions() if v != latest]})

    async def versioned(request):
        try:
            page_path(request.path_params["version"])   # raises KeyError for an unknown version
        except KeyError:
            return HTMLResponse("unknown version", status_code=404)
        return HTMLResponse(serve(request.path_params["version"]))

    return [Route("/audit/experts", newest, methods=["GET"]),
            Route("/audit/experts/index.json", index, methods=["GET"]),
            Route("/audit/experts/{version}", versioned, methods=["GET"])]
