"""Exact GROUP BY aggregation over the DuckDB cube, with a signed calculation record.

The server calculates every aggregate number in DuckDB over the typed cube, so numeric columns need
no cast, and signs the result with an HMAC record that the `verify` tool accepts. The MCP
`aggregate` tool calls `aggregate()`.

What the module checks:
  * It sets no `LIMIT` of its own. An explicit `limit` sets `truncated` to true and `total_groups`
    reports the full count.
  * The dataset, the columns and the aggregate function are checked against the cube schema and a
    function whitelist before they reach SQL.
  * Filter and HAVING values are bound as DuckDB `?` parameters.
  * Rows whose group-by key is NULL are excluded, and the count of excluded rows per dimension is in
    the result.
  * A table with an attribute-code column such as `variable_code` refuses a sum, average, minimum or
    maximum unless a filter selects exactly one code.
  * Case variants of a category (national, National, NATIONAL) collapse into one group, and
    `normalized_dimensions` lists the merged values.
  * A query with no matching rows returns `rows: []`.

Public names:
  aggregate(cube, dataset, measures, dimensions, filters, having, order, limit,
            normalize_dimensions, eav_code_col, defaulted_year_col) -> {value, receipt}
  apply_latest_year_default(...) -> (filters, defaulted, note)
  AggregateError, raised when a request fails validation
  AGGREGATE_SCHEMA, which describes the result shape
"""
from __future__ import annotations

import copy

import math
import os
import re
from typing import Any, Optional, Sequence

from surimi_data_mcp.provenance import human_check as human_check_mod
from surimi_data_mcp.provenance import explain
from surimi_data_mcp.engine.cube import Cube
from surimi_data_mcp.provenance.receipts import issue_receipt
from surimi_data_mcp.provenance.signed_tool import ToolError

# The allowed aggregate functions, as the name a caller passes and its SQL template. The template
# takes the validated, double-quoted column in `{c}`, and `count_distinct` becomes `COUNT(DISTINCT ...)`.
_AGG_FNS: dict[str, str] = {
    "sum": "SUM({c})",
    "avg": "AVG({c})",
    "count": "COUNT({c})",
    "min": "MIN({c})",
    "max": "MAX({c})",
    "count_distinct": "COUNT(DISTINCT {c})",
}

# Aggregate functions that combine magnitudes across rows. On an attribute-coded table they need a
# single attribute code, because values under different codes have different units. COUNT and
# COUNT(DISTINCT) tally rows and work across codes.
_MAGNITUDE_FNS = {"sum", "avg", "min", "max"}

# The comparison operators that filters and HAVING clauses accept. The operator is checked against
# these sets and only the value is bound as a parameter.
_SCALAR_OPS = {"=", "!=", "<>", "<", "<=", ">", ">="}
_SET_OPS = {"in", "not in"}

# The default name of the attribute-code column, which the AER tables use.
_DEFAULT_EAV_CODE_COL = "variable_code"


def _latest_time_value(cube: Cube, dataset: str, time_column: str) -> Any:
    """Return `MAX(time_column)` for `dataset`, or None when the table is empty or has no such column.

    A missing column raises `duckdb.BinderException`, which this function turns into None.
    """
    import duckdb
    try:
        rows = cube.query(f"SELECT MAX({_q(time_column)}) AS m FROM {_q(dataset)}")
    except duckdb.BinderException:
        # The dataset has no such time column.
        return None
    return rows[0]["m"] if rows and rows[0].get("m") is not None else None


def _time_span(cube: Cube, dataset: str, time_column: str) -> tuple:
    """Return `(MIN, MAX)` of `time_column` for `dataset`, or `(None, None)` when there is no such column.

    `_disclose_summed_window` uses the span to state which years a sum covered.
    """
    import duckdb
    try:
        rows = cube.query(
            f"SELECT MIN({_q(time_column)}) AS lo, MAX({_q(time_column)}) AS hi "
            f"FROM {_q(dataset)}")
    except duckdb.BinderException:
        return (None, None)
    if not rows:
        return (None, None)
    return (rows[0].get("lo"), rows[0].get("hi"))


def apply_latest_year_default(
    cube: Cube,
    dataset: str,
    time_column: str,
    measures: Sequence[dict],
    filters: Optional[Sequence[dict]],
    dimensions: Optional[Sequence[str]],
    stock_cols: set[str],
) -> tuple[list[dict], bool, Optional[str]]:
    """Add a filter on the latest `time_column` value when a stock measure would be summed across years.

    A stock measure, such as a vessel count, is one value per year. A sum, average, minimum or
    maximum of it with no single year pinned combines every year. In that case the function adds an
    `=` filter on the latest year and returns a note that says so, as `(filters, True, note)`. Other
    measures, and queries that filter the time column with `=` or `in` or group by it, get
    `(filters, False, None)`.
    """
    base = list(filters or [])
    # Only sum, avg, min and max are guarded. A count or count_distinct tallies rows, so it is not
    # limited to one year.
    is_stock_magnitude = any(
        m.get("fn") in _MAGNITUDE_FNS and m.get("col") in stock_cols for m in measures)
    if not is_stock_magnitude:
        return base, False, None
    time_pinned = any(
        f.get("col") == time_column and f.get("op") in ("=", "in") for f in base)
    time_grouped = time_column in (dimensions or [])
    if time_pinned or time_grouped:
        return base, False, None
    latest = _latest_time_value(cube, dataset, time_column)
    if latest is None:
        return base, False, None
    new_filters = base + [{"col": time_column, "op": "=", "value": latest}]
    note = (f"Aggregated for {latest}, the latest year (this is a per-year stock; "
            f"summing it across years would double-count). The data spans multiple years; "
            f"name a year to override.")
    return new_filters, True, note


AGGREGATE_SCHEMA: dict[str, Any] = {
    "description": (
        "Deterministic GROUP BY result computed by DuckDB over the cube, with a "
        "signed calculation record. Numbers are engine-computed and complete (no silent LIMIT)."
    ),
    "value": {
        "rows": "list of group rows; dimension columns + one key per measure "
                "named '<col>_<fn>'",
        "row_count": "number of rows returned",
        "total_groups": "total groups before any explicit limit (== row_count "
                         "when not truncated)",
        "truncated": "true iff an explicit limit dropped groups from the result",
        "null_dimensions_excluded": "per-dimension count of rows dropped because "
                                    "their group-by key was NULL (never bucketed)",
        "normalized_dimensions": "per normalized dimension: canonical key -> list "
                                 "of original variants merged into it",
        "notes": "human-readable provenance/exclusion notes",
        "provenance": "dataset + measures + dimensions + applied SQL shape",
    },
    "receipt": "signed calculation record, verifiable with the `verify` tool",
}


class AggregateError(ValueError):
    """Raised on invalid aggregation requests (bad dataset/column/fn, EAV rule)."""


# Identifier validation.

def _dataset_columns(cube: Cube, dataset: str) -> dict[str, str]:
    """Return `{column_name: data_type}` for `dataset`, or raise `AggregateError` when it is not in the cube.

    The dataset name is checked against `cube.datasets()`, so a name that is not a real dataset never reaches SQL.
    """
    if dataset not in set(cube.datasets()):
        raise AggregateError(f"unknown dataset {dataset!r}")
    cols = {c["name"]: c["type"] for c in cube.columns(dataset)}
    if not cols:
        raise AggregateError(f"dataset {dataset!r} has no columns")
    return cols


def _require_column(col: str, columns: dict[str, str], role: str) -> str:
    """Return `col` when it is a column in `columns`, and raise `AggregateError` with the valid columns otherwise."""
    if not isinstance(col, str) or col not in columns:
        raise AggregateError(
            f"unknown {role} column {col!r}; valid columns: {sorted(columns)}")
    return col


def _q(identifier: str) -> str:
    """Double-quote an identifier for SQL. Callers pass only names already checked against the cube schema."""
    return '"' + identifier.replace('"', '""') + '"'


def _measure_alias(col: str, fn: str) -> str:
    """Return the output column name of a measure, for example `landings_tonnes_sum`."""
    return f"{col}_{fn}"


# Guard for attribute-coded (EAV) tables.

def _selected_codes(filters: Sequence[dict], code_col: str) -> Optional[set]:
    """Return the set of codes that `=` or `in` filters on `code_col` select, or None when none of them filters it.

    When several such filters exist, the last one decides.
    """
    selected: Optional[set] = None
    for f in filters:
        if f.get("col") != code_col:
            continue
        op = str(f.get("op", "=")).lower()
        val = f.get("value")
        if op == "=":
            selected = {val}
        elif op == "in" and isinstance(val, (list, tuple)):
            selected = set(val)
    return selected


def _enforce_eav(
    columns: dict[str, str],
    measures: Sequence[dict],
    filters: Sequence[dict],
    code_col: str,
) -> None:
    """Raise `AggregateError` when a sum, average, minimum or maximum runs on a table with `code_col` and no single code is selected.

    Values under different attribute codes have different units. A table without `code_col` and a
    query with only count measures pass.
    """
    if code_col not in columns:
        return

    needs_single_code = any(
        str(m.get("fn", "")).lower() in _MAGNITUDE_FNS for m in measures
    )
    if not needs_single_code:
        return

    codes = _selected_codes(filters, code_col)
    if codes is None or len(codes) != 1:
        raise AggregateError(
            f"dataset is EAV ({code_col!r} present): a magnitude aggregation "
            f"(sum/avg/min/max) requires a filter selecting exactly one "
            f"{code_col!r} value (got "
            f"{'none' if codes is None else sorted(codes)}). Summing across "
            f"different {code_col}s mixes incompatible units."
        )


# Filter and HAVING SQL.

def _check_finite(val: Any, where: str) -> None:
    """Raise `AggregateError` when `val` is a NaN or infinite float, before any query runs.

    Without this check the failure would come later as a plain `ValueError` from the SQL inlining in `human_check`.
    """
    if isinstance(val, float) and not math.isfinite(val):
        raise AggregateError(
            f"non-finite {where} value {val!r} is not supported")


def _build_filter_clause(
    filters: Sequence[dict],
    columns: dict[str, str],
) -> tuple[list[str], list[Any]]:
    """Return the WHERE fragments for `filters` and the parameter values that bind to them.

    The column must be in `columns` and the operator in `_SCALAR_OPS` or `_SET_OPS`, and each value
    becomes a `?` parameter.
    """
    clauses: list[str] = []
    params: list[Any] = []
    for f in filters:
        col = _require_column(f.get("col"), columns, "filter")
        op = str(f.get("op", "=")).lower()
        val = f.get("value")
        if op in _SCALAR_OPS:
            _check_finite(val, "filter")
            clauses.append(f"{_q(col)} {op} ?")
            params.append(val)
        elif op in _SET_OPS:
            if not isinstance(val, (list, tuple)) or not val:
                raise AggregateError(f"{op!r} filter needs a non-empty list value")
            for item in val:
                _check_finite(item, "filter")
            placeholders = ", ".join(["?"] * len(val))
            kw = "IN" if op == "in" else "NOT IN"
            clauses.append(f"{_q(col)} {kw} ({placeholders})")
            params.extend(val)
        else:
            raise AggregateError(f"unsupported filter operator {op!r}")
    return clauses, params


def _build_having_clause(
    having: Sequence[dict],
    measure_aliases: set[str],
) -> tuple[list[str], list[Any]]:
    """Return the HAVING fragments for `having` and the parameter values that bind to them.

    Each `agg` must be one of `measure_aliases`, and each comparison value becomes a `?` parameter.
    """
    clauses: list[str] = []
    params: list[Any] = []
    for h in having:
        agg = h.get("agg")
        if agg not in measure_aliases:
            raise AggregateError(
                f"HAVING references unknown aggregate {agg!r}; "
                f"available: {sorted(measure_aliases)}")
        op = str(h.get("op", ">")).lower()
        if op not in _SCALAR_OPS:
            raise AggregateError(f"unsupported HAVING operator {op!r}")
        _check_finite(h.get("value"), "HAVING")
        clauses.append(f"{_q(agg)} {op} ?")
        params.append(h.get("value"))
    return clauses, params


def _build_order_clause(
    order: Sequence[dict],
    dimension_keys: set[str],
    measure_aliases: set[str],
) -> str:
    """Return the ORDER BY list for `order`, where each `col` must be a dimension or measure output name."""
    allowed = dimension_keys | measure_aliases
    parts: list[str] = []
    for o in order:
        col = o.get("col")
        if col not in allowed:
            raise AggregateError(
                f"ORDER BY references unknown output {col!r}; "
                f"available: {sorted(allowed)}")
        direction = str(o.get("dir", "asc")).lower()
        if direction not in ("asc", "desc"):
            raise AggregateError(f"unsupported ORDER direction {direction!r}")
        parts.append(f"{_q(col)} {direction.upper()}")
    return ", ".join(parts)


# Core aggregation.

def _validate_measures(
    measures: Sequence[dict], columns: dict[str, str]
) -> list[tuple[str, str, str]]:
    """Return `(col, fn, sql_expression_with_alias)` for each measure, and raise `AggregateError` for a bad column or function."""
    if not measures:
        raise AggregateError("at least one measure is required")
    out: list[tuple[str, str, str]] = []
    for m in measures:
        col = _require_column(m.get("col"), columns, "measure")
        fn = str(m.get("fn", "")).lower()
        if fn not in _AGG_FNS:
            raise AggregateError(
                f"unknown aggregate function {m.get('fn')!r}; "
                f"allowed: {sorted(_AGG_FNS)}")
        c_expr = _q(col)
        # Some magnitude columns hold tokens such as 'NA', so the cube types them VARCHAR. A magnitude
        # aggregate casts with TRY_CAST to DOUBLE, which turns a non-numeric token into NULL and so
        # leaves it out of the result. count and count_distinct do not cast.
        if fn in _MAGNITUDE_FNS and str(columns.get(col, "")).upper().startswith("VARCHAR"):
            c_expr = f"TRY_CAST({_q(col)} AS DOUBLE)"
        expr = _AGG_FNS[fn].format(c=c_expr)
        alias = _measure_alias(col, fn)
        out.append((col, fn, f"{expr} AS {_q(alias)}"))
    return out


def _normalized_variant_map(
    cube: Cube, dataset: str, dim: str
) -> dict[str, list[str]]:
    """Map each lowercased value of `dim` to the list of original values that share it.

    The function reads the distinct values from the cube and skips NULLs.
    """
    rows = cube.con.execute(
        f"SELECT DISTINCT {_q(dim)} AS v FROM {_q(dataset)} "
        f"WHERE {_q(dim)} IS NOT NULL"
    ).fetchall()
    merged: dict[str, list[str]] = {}
    for (v,) in rows:
        key = str(v).lower()
        merged.setdefault(key, []).append(v)
    return merged


def _has_case_collision(cube: Cube, dataset: str, dim: str) -> bool:
    """Return True when two distinct values of `dim` are equal after lowercasing, such as 'Male' and 'male'.

    The function reads the distinct values from the cube and skips NULLs.
    """
    rows = cube.con.execute(
        f"SELECT {_q(dim)} AS v FROM {_q(dataset)} "
        f"WHERE {_q(dim)} IS NOT NULL GROUP BY {_q(dim)}"
    ).fetchall()
    seen: set = set()
    for (v,) in rows:
        key = str(v).lower()
        if key in seen:
            return True
        seen.add(key)
    return False


def _aggregate_impl(
    cube: Cube,
    dataset: str,
    measures: Sequence[dict],
    dimensions: Sequence[str],
    filters: Sequence[dict],
    having: Sequence[dict],
    order: Sequence[dict],
    limit: Optional[int],
    normalize_dimensions: Sequence[str],
    eav_code_col: str,
) -> dict[str, Any]:
    """Validate the request, run the GROUP BY on the cube and return the `value` dictionary.

    `aggregate()` adds the calculation record. The notes in the result state the NULL keys that were
    excluded, a truncation, collapsed case variants, an all-zero result, and values that were
    withheld from a total.
    """
    columns = _dataset_columns(cube, dataset)

    # Check the attribute-code rule before any query runs.
    _enforce_eav(columns, measures, filters, eav_code_col)

    # Check the measures and collect their aliases for HAVING and ORDER.
    measure_specs = _validate_measures(measures, columns)
    measure_aliases = {_measure_alias(col, fn) for col, fn, _ in measure_specs}

    # Check the dimensions and decide which ones collapse case variants.
    normalize_set = set(normalize_dimensions or [])
    for d in normalize_set:
        if d not in (dimensions or []):
            raise AggregateError(
                f"normalize_dimensions {d!r} is not in dimensions {list(dimensions)}")

    # A text dimension whose values collide under lower(), such as Male, male and MALE, collapses
    # even when the caller did not list it. A column with no collision, such as ESP and GRC, is left as it is.
    for d in dimensions or []:
        if d in normalize_set:
            continue
        if str(columns.get(d, "")).upper().startswith("VARCHAR") and \
                _has_case_collision(cube, dataset, d):
            normalize_set.add(d)

    dim_select: list[str] = []   # SELECT list entries for dimensions
    dim_group: list[str] = []    # GROUP BY entries
    dim_keys: list[str] = []     # output key names (for each dimension)
    normalized_report: dict[str, dict[str, list[str]]] = {}

    for d in dimensions or []:
        _require_column(d, columns, "dimension")
        if d in normalize_set:
            # Group by LOWER(dim), return the lowercased value, and record which variants merged.
            expr = f"LOWER({_q(d)})"
            dim_select.append(f"{expr} AS {_q(d)}")
            dim_group.append(expr)
            normalized_report[d] = _normalized_variant_map(cube, dataset, d)
        else:
            dim_select.append(_q(d))
            dim_group.append(_q(d))
        dim_keys.append(d)

    # WHERE clause: the caller's filters, plus a NOT NULL test on every dimension.
    where_clauses, params = _build_filter_clause(filters, columns)
    null_excluded: dict[str, int] = {}
    for d in dimensions or []:
        where_clauses.append(f"{_q(d)} IS NOT NULL")

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Count the rows that each dimension excluded because its key was NULL. This is a separate
    # COUNT over the caller's filters and `dimension IS NULL`.
    for d in dimensions or []:
        nclauses, nparams = _build_filter_clause(filters, columns)
        nclauses.append(f"{_q(d)} IS NULL")
        nsql = " WHERE " + " AND ".join(nclauses)
        n = cube.con.execute(
            f"SELECT COUNT(*) FROM {_q(dataset)}{nsql}", nparams
        ).fetchone()[0]
        null_excluded[d] = int(n)

    select_list = ", ".join(dim_select + [expr for _, _, expr in measure_specs])
    group_sql = (" GROUP BY " + ", ".join(dim_group)) if dim_group else ""

    having_clauses, having_params = _build_having_clause(having, measure_aliases)
    having_sql = (" HAVING " + " AND ".join(having_clauses)) if having_clauses else ""

    base_sql = (
        f"SELECT {select_list} FROM {_q(dataset)}"
        f"{where_sql}{group_sql}{having_sql}"
    )
    base_params = params + having_params

    # `total_groups` counts the groups after GROUP BY and HAVING and before any limit.
    total_groups = int(cube.con.execute(
        f"SELECT COUNT(*) FROM ({base_sql}) AS _grp", base_params
    ).fetchone()[0])

    order_sql = ""
    order_clause = _build_order_clause(order, set(dim_keys), measure_aliases)
    if order_clause:
        order_sql = " ORDER BY " + order_clause

    truncated = False
    limit_sql = ""
    if limit is not None:
        if not isinstance(limit, int) or limit < 0:
            raise AggregateError(f"limit must be a non-negative int, got {limit!r}")
        limit_sql = f" LIMIT {int(limit)}"
        truncated = total_groups > limit

    final_sql = base_sql + order_sql + limit_sql
    cur = cube.con.execute(final_sql, base_params)
    col_names = [c[0] for c in cur.description]
    rows = [dict(zip(col_names, r)) for r in cur.fetchall()]

    # A query with no dimensions over zero matching rows still returns one row of NULL aggregates.
    # When no row passes the WHERE clause, the result is emptied and `total_groups` is set to 0.
    # A grouped query returns no rows by itself, so this applies only without dimensions.
    if not dim_group and rows:
        matched = int(cube.con.execute(
            f"SELECT COUNT(*) FROM {_q(dataset)}{where_sql}", params
        ).fetchone()[0])
        if matched == 0:
            rows = []
            total_groups = 0
            truncated = False

    notes_parts: list[str] = []
    total_null = sum(null_excluded.values())
    if total_null:
        notes_parts.append(
            f"{total_null} row(s) with a NULL group-by key were excluded "
            f"(not reported -> never bucketed as 'Unknown')."
        )
    if truncated:
        notes_parts.append(
            f"truncated to {limit} of {total_groups} groups (explicit limit)."
        )
    if normalized_report:
        notes_parts.append(
            f"case-variant categories collapsed for: {sorted(normalized_report)}."
        )

    # When a sum or average comes out zero or NULL in every row and every matched row holds only zero
    # or NULL, the zero is probably unreported data. The note says so and tells the caller to check
    # another period or filter, because the server cannot tell an unreported zero from a measured one.
    if rows and _MAGNITUDE_FNS:
        for col, fn, _expr in measure_specs:
            # `measure_specs` holds a SQL fragment, and the result rows are keyed by the alias.
            alias = _measure_alias(col, fn)
            if fn not in ("sum", "avg"):
                continue
            # Skip the note when any result row has a nonzero value, because one zero group among
            # several is ordinary.
            if any(r.get(alias) not in (0, 0.0, None) for r in rows):
                continue
            nonzero = int(cube.con.execute(
                f"SELECT COUNT(*) FROM {_q(dataset)}{where_sql} "
                f"{'AND' if where_sql else 'WHERE'} {_q(col)} IS NOT NULL "
                f"AND {_q(col)} <> 0", params
            ).fetchone()[0])
            if nonzero:
                continue
            matched_rows = int(cube.con.execute(
                f"SELECT COUNT(*) FROM {_q(dataset)}{where_sql}", params
            ).fetchone()[0])
            if matched_rows:
                notes_parts.append(
                    f"{matched_rows} row(s) matched but no value was reported for "
                    f"'{col}': every one is zero or null, so this 0 means "
                    f"UNREPORTED, not measured zero. Check another period or "
                    f"filter before reading it as a real number."
                )
                # Name the sibling year columns that hold data under the same filters, in one scan.
                for populated in _populated_year_columns(
                        cube, dataset, columns, col, where_sql, params):
                    notes_parts.append(populated)
                    break

    # A total that skips rows with no reported value is a lower bound. EU_DCF_Catches.csv writes the
    # placeholder 'C' for a confidential value and lists the affected fields in a `confidential`
    # column ('A' all withheld, 'V' value withheld, 'N' nothing withheld). The loader maps 'C' to
    # NULL, and SUM skips NULL. The count of skipped rows goes into the result and the notes, where
    # the LLM reads it.
    suppressed: dict[str, int] = {}
    suppressed_by_flag: dict[str, int] = {}
    has_flag = "confidential" in {c["name"] for c in cube.columns(dataset)}
    def _dropped(col: str) -> str:
        """Return the SQL test for the rows that the aggregate skipped for `col`.

        A VARCHAR column is summed as `TRY_CAST(col AS DOUBLE)`, which turns a token such as 'C'
        into NULL at query time, so the test applies the same cast. Any other column is tested for NULL.
        """
        if str(columns.get(col, "")).upper().startswith("VARCHAR"):
            return f"TRY_CAST({_q(col)} AS DOUBLE) IS NULL"
        return f"{_q(col)} IS NULL"

    for col, fn, _expr in measure_specs:
        if fn not in _MAGNITUDE_FNS:
            continue
        n = int(cube.con.execute(
            f"SELECT COUNT(*) FROM {_q(dataset)}{where_sql} "
            f"{'AND' if where_sql else 'WHERE'} {_dropped(col)}", params
        ).fetchone()[0])
        suppressed[col] = n
    withheld = sorted(c for c, n in suppressed.items() if n)
    if withheld and has_flag:
        for flag, n in cube.con.execute(
            f"SELECT {_q('confidential')}, COUNT(*) FROM {_q(dataset)}{where_sql} "
            f"{'AND' if where_sql else 'WHERE'} "
            + " AND ".join(_dropped(c) for c in withheld)
            + f" GROUP BY {_q('confidential')}", params
        ).fetchall():
            if flag is not None:
                suppressed_by_flag[str(flag)] = int(n)
    if withheld:
        detail = ", ".join(f"{c}: {suppressed[c]}" for c in withheld)
        notes_parts.append(
            f"This result is a LOWER BOUND for a sum. Rows matched the filters but "
            f"reported no value ({detail})"
            + (f", flagged confidential {suppressed_by_flag}" if suppressed_by_flag
               else "")
            + ". Those rows are excluded from the calculation, so a sum is lower than "
              "the real total and an average, minimum or maximum covers only the rows "
              "that reported a value. State the number AND the excluded count; never "
              "present it as complete."
        )

    value: dict[str, Any] = {
        "rows": rows,
        "row_count": len(rows),
        "total_groups": total_groups,
        "truncated": truncated,
        "null_dimensions_excluded": null_excluded,
        "suppressed": suppressed,
        "suppressed_by_flag": suppressed_by_flag,
        "notes": " ".join(notes_parts) if notes_parts else "ok",
        "provenance": {
            "dataset": dataset,
            "measures": [{"col": c, "fn": f} for c, f, _ in measure_specs],
            "dimensions": list(dimensions or []),
            "row_count": len(rows),
            "total_groups": total_groups,
            "truncated": truncated,
            "null_dimensions_excluded": null_excluded,
            "human_check": human_check_mod.build_human_check(
                final_sql, base_params, dataset),
        },
    }
    if normalized_report:
        value["normalized_dimensions"] = normalized_report
    return value


def _get_secret() -> bytes:
    """Return `SURIMI_RECEIPT_SECRET` as bytes, read at call time, or raise `ToolError` when it is empty.

    `signed_tool._get_secret` behaves in the same way.
    """
    raw = os.environ.get("SURIMI_RECEIPT_SECRET", "")
    if not raw:
        raise ToolError(
            "SURIMI_RECEIPT_SECRET env var not set; cannot sign calculation records")
    return raw.encode("utf-8")


_TIME_COLS = ("year", "period", "time")


def _disclose_summed_window(
    cube: Cube,
    dataset: str,
    measures: Sequence[dict],
    dimensions: Optional[Sequence[str]],
    filters: Optional[Sequence[dict]],
    value: dict,
) -> None:
    """Add `years_covered` and a note to `value` when a sum has no year filter or year grouping.

    Such a sum adds every year in the table. The function looks for the first column of `_TIME_COLS`
    that has a span of more than one value, and writes that span as `years_covered` and appends a
    note to `notes`. It changes `value` in place and leaves the existing keys as they are, except
    `notes`, which is extended. `query_metric` makes the same disclosure from the metric's additivity.
    """
    if not any(m.get("fn") == "sum" for m in (measures or [])):
        return
    dims = set(dimensions or [])
    pinned = {f.get("col") for f in (filters or [])
              if f.get("op") in ("=", "in", "<", "<=", ">", ">=", "between")}
    for col in _TIME_COLS:
        if col in dims or col in pinned:
            return
    for col in _TIME_COLS:
        lo, hi = _time_span(cube, dataset, col)
        if lo is None or hi is None or lo == hi:
            continue
        value["years_covered"] = f"{lo}-{hi}"
        prev = value.get("notes") or ""
        if prev.strip().lower() == "ok":
            prev = ""
        value["notes"] = (
            prev + f" Summed over {lo}-{hi}, every {col} in the table. State that "
            f"period with the number, or filter/group by {col} for a single one."
        ).strip()
        return


_YEAR_MEASURE = re.compile(r"^(?P<stem>.*?)y_(?P<year>\d{4})$")


def _populated_year_columns(cube, dataset, columns, col, where_sql, params):
    """Yield one note that names the `y_YYYY` columns holding data under the same filters as `col`.

    The function counts all year columns in a single SELECT. It yields nothing when `col` is not a
    year column or no other year column has data.
    """
    m = _YEAR_MEASURE.match(col)
    if not m:
        return
    siblings = [c for c in columns
                if c != col and _YEAR_MEASURE.match(c)]
    if not siblings:
        return
    probes = ", ".join(
        f"SUM(CASE WHEN {_q(c)} IS NOT NULL AND {_q(c)} <> 0 THEN 1 ELSE 0 END)"
        for c in siblings)
    try:
        counts = cube.con.execute(
            f"SELECT {probes} FROM {_q(dataset)}{where_sql}", params).fetchone()
    except Exception:
        return
    have = sorted(c for c, n in zip(siblings, counts) if n)
    if not have:
        return
    shown = have if len(have) <= 12 else have[:6] + ["..."] + have[-3:]
    yield (f"Under these same filters the year columns that DO carry data are: "
           f"{', '.join(shown)}. Re-run against one of those instead of "
           f"'{col}'.")


def aggregate(
    cube: Cube,
    dataset: str,
    measures: Sequence[dict],
    dimensions: Optional[Sequence[str]] = None,
    filters: Optional[Sequence[dict]] = None,
    having: Optional[Sequence[dict]] = None,
    order: Optional[Sequence[dict]] = None,
    limit: Optional[int] = None,
    normalize_dimensions: Optional[Sequence[str]] = None,
    eav_code_col: str = _DEFAULT_EAV_CODE_COL,
    defaulted_year_col: Optional[str] = None,
) -> dict[str, Any]:
    """Run an exact GROUP BY aggregation on a cube dataset and sign the result.

    Args:
        cube: a built `Cube`.
        dataset: the dataset name, checked against the cube schema.
        measures: a list of `{"col": <column>, "fn": <sum|avg|count|min|max|count_distinct>}`.
            The output keys are `<col>_<fn>`.
        dimensions: the group-by columns. Rows with a NULL key are excluded and counted.
        filters: a list of `{"col", "op", "value"}`, with each value bound as a parameter.
        having: a list of `{"agg": <measure alias>, "op", "value"}`.
        order: a list of `{"col": <dimension or measure alias>, "dir": asc|desc}`.
        limit: the maximum number of groups. A limit that drops groups sets `truncated` to true, and
            `total_groups` holds the full count.
        normalize_dimensions: dimensions whose case variants collapse into one group.
        eav_code_col: the attribute-code column, `variable_code` by default.
        defaulted_year_col: the time column that the caller pinned to the latest year, which the
            explanation sentence mentions.

    Returns:
        `{"value": {...}, "receipt": {...}}`, where the `verify` tool accepts the signed `receipt`.

    Raises:
        AggregateError: for an unknown dataset, column or function, a violation of the
            attribute-code rule, or a malformed filter, having or order entry.
        ToolError: when `SURIMI_RECEIPT_SECRET` is not set.
    """
    # The record's `input_params` hold the logical query and not the cube connection. The secret is
    # read on each call, and the signature covers the computed output, so a changed number fails
    # verification.
    secret = _get_secret()

    value = _aggregate_impl(
        cube,
        dataset,
        measures,
        dimensions or [],
        filters or [],
        having or [],
        order or [],
        limit,
        normalize_dimensions or [],
        eav_code_col,
    )

    _disclose_summed_window(cube, dataset, measures, dimensions, filters, value)

    input_params: dict[str, Any] = {
        "dataset": dataset,
        "measures": [dict(m) for m in measures],
        "dimensions": list(dimensions or []),
        "filters": [dict(f) for f in (filters or [])],
        "having": [dict(h) for h in (having or [])],
        "order": [dict(o) for o in (order or [])],
        "limit": limit,
        "normalize_dimensions": list(normalize_dimensions or []),
    }

    # A sentence on how the number was calculated, for the person reading the answer. It is built
    # after the disclosures above so that it can state them, and it goes only into the record's
    # provenance and not into `value`.
    provenance = copy.deepcopy(value.get("provenance"))
    if isinstance(provenance, dict) and isinstance(provenance.get("human_check"), dict):
        provenance["human_check"]["explanation"] = explain.explain_aggregate(
            dataset, measures, dimensions or [], filters or [], having=having or [], order=order or [],
            limit=limit, value=value, defaulted_year_col=defaulted_year_col)
    receipt = issue_receipt(
        tool_id="aggregate",
        input_params=input_params,
        output_value=value,
        secret=secret,
        provenance=provenance,
    )
    return {"value": value, "receipt": receipt}
