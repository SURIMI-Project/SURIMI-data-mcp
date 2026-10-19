"""Tools that say what exists: data sources, datasets, columns, tables, metrics and data quality.
"""
from typing import Optional

from surimi_data_mcp import capabilities
from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.datasets import metrics as metrics_module
from surimi_data_mcp.datasets import quality as quality_module
from surimi_data_mcp.engine import cube as cube_module
from surimi_data_mcp.mcp_server import log, mcp
from surimi_data_mcp.tools.common import get_trino, resolve_cube_dataset

# ── Meta tools ──

@mcp.tool()
async def describe_data_capabilities() -> dict:
    """List all data sources, tools, and usage hints for this SURIMI data MCP server."""
    log("[meta] describe_data_capabilities")
    tools = await mcp.list_tools()
    tool_names = [t.name for t in tools]
    return capabilities.describe(tool_names)


# ── Lookup tools ──

@mcp.tool()
def list_datasets(domain: Optional[str] = None) -> dict:
    """List available datasets. Optionally filter by domain (e.g. 'oecd', 'eu_dcf', 'surimi', 'fisheries').

    `documented_use` says what SURIMI's documentation records about what the data was used for."""
    log(f"[lookup] list_datasets domain={domain}")
    out = catalog.list_datasets(domain=domain)
    # The catalogue is a hand-written list and `aggregate` checks names against
    # the cube, so mark the datasets the cube cannot serve. If the cube is
    # unavailable, nothing is marked.
    try:
        cube = cube_module.get_cube()
        names = set(cube.datasets())
        out = {**out, "datasets": catalog.mark_unservable(
            out["datasets"], lambda t: resolve_cube_dataset(t, cube), names)}
    except Exception as exc:              # never fail a listing over this
        log(f"[lookup] servable reconciliation skipped: {exc}")
    return out


@mcp.tool()
def list_columns(dataset_id: str) -> dict:
    """Show columns and types for a dataset. Accepts the dataset id, the table name, or the full
    table name (hive.<schema>.<table>).

    Columns are read live from the database, so they match the real table. The static catalogue is
    used only when the database cannot be reached or returns no columns.
    """
    log(f"[lookup] list_columns dataset_id={dataset_id}")
    ds = catalog.find_dataset(dataset_id)
    if ds is None:
        return catalog.list_columns(dataset_id=dataset_id)  # carries the not-found error
    try:
        live = get_trino().describe_table(ds["table"])
        if live.get("columns") and not live.get("error"):
            return catalog.list_columns(ds["id"], columns=live["columns"])
    except Exception as e:  # DB down / not yet loaded -> static fallback
        log(f"[lookup] list_columns live lookup failed ({e}); using static catalog")
    return catalog.list_columns(dataset_id=dataset_id)


# ── Fetcher tools ──

# `query_data`, the only tool that runs SQL, is in figures.py and signs its result.


@mcp.tool()
def describe_table(table: str) -> dict:
    """Show columns and types for a database table (e.g. 'hive.eu_dcf.fleet_economics'; the
    hive. prefix is accepted on PostgreSQL too). A table the database does not hold may still be
    served by query_data from the cube, so a miss here is not an absence: prefer list_columns."""
    log(f"[fetcher] describe_table table={table}")
    client = get_trino()
    return client.describe_table(table)


@mcp.tool(name="list_metrics")
def list_metrics() -> dict:
    """List every named metric the `query_metric` tool accepts, with additivity
    semantics (stock/flow/ratio), unit, and allowed dimensions. Call this first
    to discover metric names, then call `query_metric` for numbers."""
    log("[metrics] list_metrics")
    return {
        "metrics": [
            {
                "name": m.name,
                "label": m.label,
                "unit": m.unit,
                "additivity": m.additivity,
                "dimensions": list(m.dimensions),
            }
            for m in metrics_module.REGISTRY
        ],
        "prompt": metrics_module.catalog_prompt(),
    }


@mcp.tool(name="describe_quality")
def describe_quality(dataset_id: str) -> dict:
    """Per-column data-quality summary for a dataset: non-null count and null
    fraction, the count of the confidentiality sentinel ('C'), a distinct
    count for categorical columns, and (when a `year` column exists) the
    years present and missing inside the dataset's own span.

    Accepts a dataset id, table name, or full table name (hive.<schema>.<table>), same as
    `aggregate`.
    Use it for null rates, confidentiality suppression and year-coverage gaps.
    """
    log(f"[lookup] describe_quality dataset_id={dataset_id}")
    cube = cube_module.get_cube()
    resolved = resolve_cube_dataset(dataset_id, cube)
    try:
        return quality_module.describe_quality(cube, resolved)
    except quality_module.QualityError as e:
        return {"error": str(e)}
