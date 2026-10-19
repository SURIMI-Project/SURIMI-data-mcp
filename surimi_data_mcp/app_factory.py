"""Builds the ASGI app that serves both MCP transports from one process.

open-webui's MCP client uses Streamable HTTP, which the app serves at `/mcp`. SSE stays at `/sse` and
`/messages` for older clients. Both run in one Starlette app with a combined lifespan, so both FastMCP
session managers start and stop together.
"""
from __future__ import annotations

import contextlib
import logging

import anyio
from starlette.applications import Starlette

from surimi_data_mcp.engine import cube as cube_module
from surimi_data_mcp.provenance import lineage
from surimi_data_mcp.web.audit_routes import make_audit_routes
from surimi_data_mcp.web.experts_routes import make_experts_routes
from surimi_data_mcp.web import lake_cache
from surimi_data_mcp.web.lake_routes import make_lake_routes
from surimi_data_mcp.web.web_auth import make_web_routes
from surimi_data_mcp.web.sources_routes import make_sources_routes
from surimi_data_mcp.engine.sql_tools import CubeFallbackClient
from surimi_data_mcp.engine.databases import TrinoClient, default_client

# A private copy of `get_trino()` and `query_client()` from `tools/common.py`, not an import, so that
# mounting the audit routes does not register the MCP tools. It builds the same client as the
# `query_data` tool (primary backend with the DuckDB cube fallback), so a `query_data` receipt
# recomputes the same way here and through the MCP `verify` tool.
_trino: TrinoClient | None = None


def _get_trino() -> TrinoClient:
    global _trino
    if _trino is None:
        _trino = default_client()
    return _trino


def _query_client() -> CubeFallbackClient:
    """Return the `query_data` client: the primary backend with the DuckDB cube fallback."""
    return CubeFallbackClient(
        _get_trino(), cube_module.get_cube(), cube_module._sanitize_name,
    )


def build_asgi_app(mcp, *, http_path: str = "/mcp", sse_path: str = "/sse") -> Starlette:
    """Return a Starlette app that serves `mcp` over streamable HTTP at `http_path` and over SSE at `sse_path` and `/messages`.

    The same app serves the sign-in, `/sources`, `/audit/experts` and `/audit` routes, under one combined
    lifespan. `/audit/verify` receives `cube_factory` for the `aggregate` recompute and
    `query_client_factory` for the `query_data` recompute.
    """
    http_app = mcp.http_app(path=http_path, transport="http")
    sse_app = mcp.http_app(path=sse_path, transport="sse")

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with http_app.router.lifespan_context(app):
            async with sse_app.router.lifespan_context(app):
                # Compute the cube sha256 off the event loop, so the first aggregate call does
                # not wait for the hash. A failed hash is logged and the server runs without cube
                # lineage. `lineage._CUBE_SHA_DONE` stays False after an exception, so a later
                # `get_cube_sha256()` call tries again.
                try:
                    await anyio.to_thread.run_sync(lineage.get_cube_sha256)
                except Exception:
                    logging.getLogger(__name__).warning(
                        "cube sha256 warmup failed; serving without cube "
                        "lineage", exc_info=True)
                # Copy the data lake onto the volume in the background, so `/sources/lake` keeps
                # working when the daily S3 token expires. Does nothing without `SURIMI_LAKE_CACHE`.
                lake_cache.start_background()
                yield

    return Starlette(
        routes=[*http_app.routes, *sse_app.routes,
                # before /sources/{file}, which would otherwise take every /sources/lake/... path
                *make_web_routes(), *make_lake_routes(), *make_sources_routes(),
                *make_experts_routes(),
                *make_audit_routes(cube_factory=cube_module.get_cube,
                                   query_client_factory=_query_client)],
        lifespan=lifespan,
    )
