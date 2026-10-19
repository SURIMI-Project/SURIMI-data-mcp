"""Tools where the server runs one analysis and returns it with its calculation records: `analyze_trend`,
`compare_countries`, `generate_report` and `breakdown`.
"""
from typing import Optional

from surimi_data_mcp.datasets import catalog, wide_years
from surimi_data_mcp.engine import skills
from surimi_data_mcp.engine.aggregate import AggregateError
from surimi_data_mcp.knowledge import docsearch
from surimi_data_mcp.mcp_server import log, mcp
from surimi_data_mcp.tools.common import (
    attach_audit_urls,
    resolve_for_skill,
    wide_year_view,
)


@mcp.tool()
def analyze_trend(dataset_id: str, metric: str, group_by: Optional[str] = None,
                  period: Optional[str] = None) -> dict:
    """Run a trend analysis on the server and return it with its calculation records.

    Returns {skill, dataset, params_used, finding, data, chart, provenance, notes,
    receipts, audit_urls}. State the `finding` and cite the `receipts`.
    """
    log(f"[skill] analyze_trend dataset={dataset_id} metric={metric}")
    try:
        ds, cube, cube_ds = resolve_for_skill(dataset_id)
        time_column = ds.get("time_column")
        if time_column is None:
            wide = wide_year_view(ds, cube, cube_ds)
            if wide is None:
                return {"error": "This dataset has no time dimension; query it directly with query_data (a plain SELECT).",
                        "skill": "analyze_trend", "receipts": []}
            cube_ds, time_column, years = wide
            # The measure sits in one column per year, and the folded view
            # names that column `value`, so the caller's metric is replaced.
            metric = "value"
        out = attach_audit_urls(skills.run_analyze_trend(
            cube, dataset_name=cube_ds, time_column=time_column,
            metric=metric, group_by=group_by, period=period,
            code_labels=ds.get("code_labels")))
        if ds.get("time_column") is None and isinstance(out, dict):
            rows = (out.get("data") or {}).get("rows") or out.get("rows") or []
            dims = [group_by] if group_by else []
            key = wide_years.measure_key(rows, time_column, dims)
            blank = wide_years.empty_years(rows, time_column, key) if key else []
            if blank:
                note = (f"Years with no value anywhere in this selection: "
                        f"{blank}. Those are years the source did not cover "
                        f"here, NOT measured zeros; do not read them as no "
                        f"change.")
                # `notes` is a list or a string, so append to either.
                prev = out.get("notes")
                if isinstance(prev, list):
                    out["notes"] = prev + [note]
                else:
                    out["notes"] = ((prev or "") + " " + note).strip()
        return out
    except (ValueError, AggregateError) as e:
        return {"error": str(e), "skill": "analyze_trend", "receipts": []}


@mcp.tool()
def compare_countries(dataset_id: str, metric: str,
                      countries: Optional[list[str]] = None,
                      period: Optional[str] = None) -> dict:
    """Rank a metric across countries on the server and return the ranking with its calculation records."""
    log(f"[skill] compare_countries dataset={dataset_id} metric={metric}")
    try:
        ds, cube, cube_ds = resolve_for_skill(dataset_id)
        time_column = ds.get("time_column")
        if time_column is None:
            wide = wide_year_view(ds, cube, cube_ds)
            if wide is None:
                return {"error": "This dataset has no time dimension; query it directly with query_data (a plain SELECT).",
                        "skill": "compare_countries", "receipts": []}
            # Folded into `year` and `value`, as in `analyze_trend`.
            cube_ds, time_column, _years = wide
            metric = "value"
        return attach_audit_urls(skills.run_compare_countries(
            cube, dataset_name=cube_ds, time_column=time_column,
            metric=metric, countries=countries, period=period,
            country_col=ds.get("country_column", "country_code")))
    except (ValueError, AggregateError) as e:
        return {"error": str(e), "skill": "compare_countries", "receipts": []}


@mcp.tool()
def generate_report(dataset_id: str, focus: str,
                    period: Optional[str] = None) -> dict:
    """Compose a report from a row count, the trend of the first numeric column and the provenance passages.

    The row count and the trend come with calculation records in `receipts`. The provenance section
    adds a record for its passages only when the server has a signing key.
    """
    log(f"[skill] generate_report dataset={dataset_id} focus={focus}")
    try:
        ds, cube, cube_ds = resolve_for_skill(dataset_id)
        time_column, columns = ds.get("time_column"), ds["columns"]
        if time_column is None:
            wide = wide_year_view(ds, cube, cube_ds)
            if wide is None:
                return {"error": "This dataset has no time dimension; query it directly with query_data (a plain SELECT).",
                        "skill": "generate_report", "receipts": []}
            cube_ds, time_column, years = wide
            year_cols = {col for _, col in years}
            columns = ([c for c in columns if c["name"] not in year_cols]
                       + [{"name": "year", "type": "int"}, {"name": "value", "type": "double"}])
        import os
        secret = os.environ.get("SURIMI_RECEIPT_SECRET", "").encode("utf-8") or None
        return attach_audit_urls(skills.run_generate_report(
            cube, docsearch.get_index(), dataset_name=cube_ds,
            time_column=time_column, columns=columns, focus=focus,
            period=period, dataset_id=ds["id"], secret=secret))
    except (ValueError, AggregateError) as e:
        return {"error": str(e), "skill": "generate_report", "receipts": []}


@mcp.tool()
def breakdown(dataset_id: str, group_by: str, metric: Optional[str] = None,
              fn: Optional[str] = None, variable_code: Optional[str] = None,
              year: Optional[str] = None) -> dict:
    """Break a metric (or row count) down across a category, as a bar chart.

    Use for distribution / "X by category" / "how many per Y" questions. Counts
    rows per category by default; case-variant categories (Male/male/MALE) are
    merged automatically, and on attribute-coded (EAV) tables a magnitude needs
    one variable_code (it returns the choices instead of guessing). A per-year
    stock (vessel counts, capacity) is reported for the latest year rather than
    summed across every year; pass `year` to pick a different one. Returns the
    envelope {skill, finding, data, chart, provenance, notes, receipts} with its calculation record.
    """
    log(f"[skill] breakdown dataset={dataset_id} group_by={group_by} metric={metric}")
    try:
        ds, cube, cube_ds = resolve_for_skill(dataset_id)
        return attach_audit_urls(skills.run_breakdown(
            cube, dataset_name=cube_ds, group_by=group_by, metric=metric, fn=fn,
            variable_code=variable_code, columns=ds["columns"],
            stock_cols=catalog.stock_columns(ds["id"]),
            time_column=ds.get("time_column", "year"), year=year))
    except (ValueError, AggregateError) as e:
        return {"error": str(e), "skill": "breakdown", "receipts": []}
