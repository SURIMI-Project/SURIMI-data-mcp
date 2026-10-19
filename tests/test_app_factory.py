"""Tests for the ASGI app factory that serves BOTH transports:
streamable-http at /mcp (for open-webui native MCP + modern clients) and
SSE at /sse (legacy clients). open-webui's native MCP is streamable-http
only; mcpo's SSE bridge crashes on a known anyio teardown bug, so we serve
/mcp natively and keep /sse for backward compat."""
from __future__ import annotations

import pytest
from fastmcp import FastMCP

from surimi_data_mcp.app_factory import build_asgi_app


def _mcp() -> FastMCP:
    m = FastMCP("test-app")

    @m.tool()
    def ping() -> str:
        return "pong"

    return m


def test_app_exposes_both_transports():
    app = build_asgi_app(_mcp())
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/mcp" in paths
    assert "/sse" in paths


def test_app_includes_sse_messages_endpoint():
    app = build_asgi_app(_mcp())
    paths = {getattr(r, "path", None) for r in app.routes}
    # SSE transport needs its POST /messages companion endpoint
    assert "/messages" in paths


def test_mcp_streamable_http_initialize_works():
    from starlette.testclient import TestClient

    app = build_asgi_app(_mcp())
    with TestClient(app) as c:
        r = c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0", "method": "initialize", "id": 1,
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert r.status_code == 200
        assert "protocolVersion" in r.text


def test_custom_paths():
    app = build_asgi_app(_mcp(), http_path="/api/mcp", sse_path="/api/sse")
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/mcp" in paths
    assert "/api/sse" in paths


def test_sources_routes_mounted():
    app = build_asgi_app(_mcp())
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/sources" in paths
    assert "/sources/{file:path}" in paths


def test_audit_routes_mounted():
    from starlette.testclient import TestClient

    app = build_asgi_app(_mcp())
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/audit" in paths
    assert "/audit/verify" in paths
    assert "/audit/receipt/{signature}" in paths
    with TestClient(app) as c:
        r = c.get("/audit")
        assert r.status_code == 200
        assert "Recompute" in r.text


def test_lifespan_survives_warmup_failure(monkeypatch):
    """A failed cube-hash warmup must NOT kill boot: the server should come
    up and serve (without cube lineage) rather than crash-loop the pod."""
    from starlette.testclient import TestClient

    from surimi_data_mcp.provenance import lineage

    def boom():
        raise RuntimeError("cube unreadable")

    monkeypatch.setattr(lineage, "get_cube_sha256", boom)
    app = build_asgi_app(_mcp())
    with TestClient(app) as c:  # lifespan runs here; must not raise
        r = c.get("/sources")
        assert r.status_code == 200


def test_audit_verify_recomputes_query_data_receipts(monkeypatch):
    """Q-E11-2c: the deployed /audit/verify endpoint must recompute query_data
    receipts too, not just aggregate ones.

    app_factory used to hardcode query_client_factory=None (an import-cycle
    workaround that no longer applies), which made every query_data receipt on
    the PUBLIC audit page permanently signature-only -- even though the MCP
    `verify` tool (`tools/verify.py`) recomputes that very same tool_id. This pins
    the symmetry between the two, as `web/audit_routes.py` describes.
    """
    from starlette.testclient import TestClient

    from surimi_data_mcp import app_factory
    from surimi_data_mcp.provenance import receipts as receipts_mod

    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")

    class _FakeQueryClient:
        def execute(self, sql, limit=500):
            return {"columns": ["v"], "rows": [{"v": 42}], "row_count": 1,
                    "truncated": False, "error": None}

    monkeypatch.setattr(app_factory, "_query_client", lambda: _FakeQueryClient())

    output_value = {"columns": ["v"], "rows": [{"v": 42}], "row_count": 1,
                     "truncated": False, "error": None}
    receipt = receipts_mod.issue_receipt(
        tool_id="query_data",
        input_params={"sql": "SELECT 42 AS v", "limit": 500},
        output_value=output_value,
        secret=b"test-secret",
    )

    app = app_factory.build_asgi_app(_mcp())
    with TestClient(app) as c:
        r = c.post("/audit/verify", json={"receipt": receipt, "recompute": True})
    body = r.json()
    assert body["verified"] is True
    assert body["recompute_supported"] is True, body
    assert body["verification_method"] == "signature_and_recompute", body
    assert body["match"] is True, body


def test_lifespan_warms_cube_hash(monkeypatch):
    """Boot must precompute the cube sha256 off the event loop so the first
    aggregate call doesn't stall hashing a ~1.8 GB cube."""
    from starlette.testclient import TestClient

    from surimi_data_mcp.provenance import lineage

    calls = []
    monkeypatch.setattr(lineage, "get_cube_sha256", lambda: calls.append(1))
    app = build_asgi_app(_mcp())
    with TestClient(app):
        pass  # entering the context manager runs the lifespan
    assert calls == [1]


def test_lake_routes_come_before_the_generic_sources_route():
    """/sources/{file:path} would answer 404 for every /sources/lake/... path if it matched first."""
    app = build_asgi_app(_mcp())
    paths = [getattr(r, "path", None) for r in app.routes]
    assert paths.index("/sources/lake") < paths.index("/sources/{file:path}")
    assert paths.index("/sources/lake/{path:path}") < paths.index("/sources/{file:path}")


def test_web_sign_in_and_repo_routes_come_before_the_generic_sources_route():
    app = build_asgi_app(_mcp())
    paths = [getattr(r, "path", None) for r in app.routes]
    for p in ("/sources/auth/login", "/sources/auth/callback", "/sources/repo/{path:path}"):
        assert paths.index(p) < paths.index("/sources/{file:path}"), p
