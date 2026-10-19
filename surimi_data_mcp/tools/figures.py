"""Tools that calculate a number and sign it: `aggregate`, `query_metric` and `query_data`.
"""
from typing import Literal, Optional

from typing_extensions import TypedDict

from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.datasets import metrics as metrics_module
from surimi_data_mcp.engine import cube as cube_module
from surimi_data_mcp.engine.aggregate import (
    AGGREGATE_SCHEMA,
    AggregateError,
    apply_latest_year_default,
)
from surimi_data_mcp.engine.aggregate import aggregate as _aggregate
from surimi_data_mcp.engine.sql_tools import (
    eav_magnitude_refusal,
    is_aggregate_sql,
    landings_double_count_refusal,
    make_query_data_signed,
    read_only_refusal,
)
from surimi_data_mcp.mcp_server import log, mcp
from surimi_data_mcp.provenance.audit_link import audit_url_for
from surimi_data_mcp.tools.common import (
    attach_audit_urls,
    query_client,
    resolve_cube_dataset,
)


@mcp.tool(name="query_metric")
def query_metric(metric: str, by: Optional[list[str]] = None,
                 where: Optional[list[dict]] = None, year: Optional[int] = None,
                 order: Optional[list[dict]] = None, limit: Optional[int] = None) -> dict:
    """Compute a NAMED metric and slice it. This is the preferred way to get any
    number. The metric owns the table, aggregation, units and time rule, so you
    cannot pick the wrong table or sum a per-year stock across years. List metrics
    with list_metrics. `by` = dimensions to group by; `where` = filters
    [{col,op,value}]; `year` pins a year (stocks default to the latest year if you
    omit it). Returns the result with its calculation record (`receipt`)."""
    log(f"[metric] query_metric metric={metric} by={by} year={year}")
    try:
        cube = cube_module.get_cube()
        out = metrics_module.run_query_metric(
            cube, metric, by=by, where=where, year=year, order=order, limit=limit)
        return attach_audit_urls(out)
    except (ValueError, AggregateError) as e:
        return {"error": str(e), "tool": "query_metric",
                "metrics": [m.name for m in metrics_module.REGISTRY], "receipts": []}


# The receipt names the client that ran the query. `backend_id` is used only when a client cannot
# name itself.
_query_data_signed = make_query_data_signed(
    client_factory=query_client,
    backend_id="sql://unnamed",
)


@mcp.tool(name="query_data")
def query_data(sql: str, limit: int = 500) -> dict:
    """Run one read-only SQL SELECT against the server's database (PostgreSQL on EDITO; tables
    it lacks are served from the DuckDB cube). Returns {value, receipt, audit_url}.

    value holds rows/columns (and available_columns on a column error, for
    self-correction). receipt is the signed calculation record, an HMAC signature over the
    inputs and the output, so the number can be re-checked with the `verify` tool. A query that
    is not a single SELECT, or that would double-count, is refused with receipt null. Call
    list_columns(dataset_id) to discover column names; double-quote dot-notation columns.
    For any total / sum / average / count / ranking prefer the `aggregate` or
    `query_metric` tool (they own year-scoping + EAV handling); an aggregate SQL
    here still runs and is signed, but comes back with a steering note.
    """
    log(f"[fetcher signed] query_data sql={sql[:80]}...")
    # Three refusals run before the query, so the server never signs a number it knows is wrong.
    # The first rejects anything but a single read. The second rejects a sum or average of
    # the EAV `variable_value` column, whose rows overlap across strata. A normal column can
    # still be summed.
    refusal = read_only_refusal(sql) or eav_magnitude_refusal(sql)
    # The third rejects a sum or average that spans `fisheries_landings` and
    # `fisheries_landings_divisions`, because each division is already inside its area total.
    if not refusal:
        refusal = landings_double_count_refusal(sql)
    if refusal:
        return {
            "value": {"columns": [], "rows": [], "row_count": 0,
                      "truncated": False, "error": refusal},
            "receipt": None,
            "audit_url": None,
        }
    out = _query_data_signed(sql=sql, limit=limit)
    out["audit_url"] = audit_url_for(out.get("receipt") or {})
    # An aggregate query still runs and is signed. The steering note points the LLM to the tools
    # that scope a per-year stock to one year and bind EAV variable codes.
    if is_aggregate_sql(sql):
        out["steering"] = (
            "This is an aggregate query and the result is signed, but for any "
            "total / sum / average / ranking prefer the `aggregate` or "
            "`query_metric` tool: they scope a per-year stock to a single year "
            "(a raw GROUP BY here can silently sum across every year) and bind "
            "EAV variable codes correctly."
        )
    # The substitution warning is attached to the result, so it reaches the LLM at the call that
    # uses the table.
    _tables = catalog.tables_in_sql(sql)
    _warn = [w for w in (catalog.substitute_warning(t) for t in _tables) if w]
    if _warn:
        out["not_a_substitute_for"] = " ".join(dict.fromkeys(_warn))
    return out


# Typed so that the tool's JSON schema names the keys `col` and `fn`.
class Measure(TypedDict):
    """One measure: col is a column of the dataset, fn the aggregation."""
    col: str
    fn: Literal["sum", "avg", "count", "min", "max", "count_distinct"]


@mcp.tool(name="aggregate")
def aggregate(
    measures: list[Measure],
    dataset: Optional[str] = None,
    dataset_id: Optional[str] = None,
    dimensions: Optional[list[str]] = None,
    filters: Optional[list[dict]] = None,
    having: Optional[list[dict]] = None,
    order: Optional[list[dict]] = None,
    limit: Optional[int] = None,
    normalize_dimensions: Optional[list[str]] = None,
) -> dict:
    """Return EXACT aggregates computed by the DuckDB engine, with a signed calculation record (`receipt`).

    USE THIS TOOL FOR ANY NUMBER YOU WOULD OTHERWISE COMPUTE. For any total,
    average, count, ranking, min/max, share/percentage, or "how many / how much"
    question, you MUST call `aggregate`; never sum, average, count, or rank rows
    yourself, and never estimate or eyeball a number from query_data rows -- the
    model skips rows and mis-aggregates. Prefer `aggregate` over `query_data` for
    every numeric summary. This tool IS how you sum on this server: never tell the
    user a "restriction" prevents summing or that you "cannot compute" a total --
    if a call errors, fix the arguments (dataset, measure, one EAV variable_code,
    a year filter) and retry. Quote the returned numbers verbatim and cite the
    calculation record; the engine computes them and the `verify` tool can re-check them.

    Structured request (the engine validates every identifier against the real
    cube schema, so unknown/injected names are rejected, never run as SQL):
      * dataset: logical dataset name (call list_datasets to discover).
      * measures: list of {"col": <column>, "fn": <sum|avg|count|min|max|
        count_distinct>}. Each output key is "<col>_<fn>".
      * dimensions: GROUP BY columns. NULL keys are EXCLUDED (never bucketed as
        "Unknown") and the count of excluded rows is reported.
      * filters: list of {"col","op","value"} (op in =,!=,<,<=,>,>=,in,not in);
        values are parametrized, so a "'; DROP" value is data, never SQL.
      * having: list of {"agg": <measure alias>, "op", "value"}.
      * order: list of {"col": <dimension or measure alias>, "dir": asc|desc}.
      * limit: explicit cap ONLY. There is no silent LIMIT on aggregates; if a
        cap drops groups the result sets truncated:true and total_groups reports
        the full count, so a ranking is never silently cut.
      * normalize_dimensions: dimensions whose case variants (national/National/
        NATIONAL) should collapse; the merge is reported.

    EAV tables (an attribute-code column such as variable_code, e.g. AER) refuse
    a magnitude sum/avg unless a filter selects exactly one code -- summing
    employment_fte with landings_tonnes is meaningless.

    Returns {value, receipt} on success (value carries rows, row_count,
    total_groups, truncated, null_dimensions_excluded, notes, provenance), or
    {error, value: null, receipt: null} on an invalid request -- no number is
    ever fabricated on error.
    """
    # Other tools take `dataset_id`, so this tool accepts both spellings.
    dataset = dataset or dataset_id
    if not dataset:
        return {"error": "dataset is required (dataset_id is accepted too)"}
    log(f"[aggregate] dataset={dataset} measures={measures} dims={dimensions}")
    try:
        cube = cube_module.get_cube()
        # The chat passes catalog ids / Trino FQNs; map them to the cube's stem.
        resolved = resolve_cube_dataset(dataset, cube)
        ds_meta = catalog.find_dataset(dataset)
        stock_cols = catalog.stock_columns(ds_meta["id"]) if ds_meta else set()
        time_col = (ds_meta or {}).get("time_column", "year")
        filters, year_defaulted, default_note = apply_latest_year_default(
            cube, resolved, time_col, measures, filters, dimensions, stock_cols)
        out = _aggregate(
            cube,
            dataset=resolved,
            measures=measures,
            dimensions=dimensions,
            filters=filters,
            having=having,
            order=order,
            limit=limit,
            normalize_dimensions=normalize_dimensions,
            defaulted_year_col=time_col if year_defaulted else None,
        )
        if year_defaulted:
            out["value"]["year_defaulted"] = True
            notes = out["value"].get("notes") or ""
            if notes.strip().lower() == "ok": notes = ""
            out["value"]["notes"] = (notes + " " + default_note).strip()
        # A receipt covers the arithmetic only, so the result also says which kind of table the
        # number came from.
        if ds_meta:
            out["value"]["data_kind"] = ds_meta.get("kind", "")
            _sub = catalog.substitute_warning(ds_meta["id"])
            if _sub:
                out["value"]["not_a_substitute_for"] = _sub
            _mnote = catalog.modelled_note(ds_meta["id"])
            if _mnote:
                _n = out["value"].get("notes") or ""
                if _n.strip().lower() == "ok": _n = ""
                out["value"]["notes"] = (_n + " " + _mnote).strip()
        out["audit_url"] = audit_url_for(out.get("receipt") or {})
        return out
    except AggregateError as e:
        # An invalid request returns an error with no value and no receipt.
        log(f"[aggregate] rejected: {e}")
        return {"error": str(e), "value": None, "receipt": None}


# The result schema of `aggregate`, for clients and tests that read it without running a query.
AGGREGATE_RESULT_SCHEMA = AGGREGATE_SCHEMA
