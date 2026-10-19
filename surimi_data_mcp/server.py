"""SURIMI data server entry point: registers the tools on the MCP server and starts it.

The tools are in `surimi_data_mcp.tools`, grouped by what they do, and the data sources they read are
declared in `surimi_data_mcp.sources`. `python -m surimi_data_mcp` runs `main()`.
"""
from surimi_data_mcp import (
    capabilities,
    sources,  # noqa: F401  (declares the data sources)
)
from surimi_data_mcp.mcp_server import log, mcp
from surimi_data_mcp.tools import analyses, discovery, explain, figures, verify  # noqa: F401  (registers the tools)

PORT = 26610
HOST = "0.0.0.0"


# ── Startup ──

def main() -> None:
    """Run the drift check, then serve both MCP transports and the HTTP routes with uvicorn.

    The drift check compares the registered tool names with the tools that `capabilities` attributes to
    sources, and logs a warning for each difference.
    """
    import asyncio

    import uvicorn

    from surimi_data_mcp.app_factory import build_asgi_app

    log("running capabilities drift check...")
    tools = asyncio.run(mcp.list_tools())
    drift = capabilities.drift_check({t.name for t in tools})
    if drift["missing_from_registry"]:
        log(f"WARN drift: tools not claimed by any source: {drift['missing_from_registry']}")
    if drift["missing_from_fastmcp"]:
        log(f"WARN drift: claimed tools missing from FastMCP: {drift['missing_from_fastmcp']}")
    if not drift["missing_from_registry"] and not drift["missing_from_fastmcp"]:
        log(f"drift check OK ({len(tools)} tools, {len(capabilities.all_capabilities())} sources)")

    log(f"starting MCP server on {HOST}:{PORT} (streamable-http at /mcp, sse at /sse)")
    app = build_asgi_app(mcp, http_path="/mcp", sse_path="/sse")
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
