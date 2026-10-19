"""The per-column data-quality summary behind the `describe_quality` tool.

For each column of a cube dataset it counts the non-null rows, the null fraction, the rows that hold
the confidentiality sentinel and, for text and boolean columns, the distinct values. If the dataset has
a `year` column it also reports the first and last year and the years missing between them.
"""
from __future__ import annotations

from typing import Any, Optional

from surimi_data_mcp.engine.cube import Cube

# The marker the EU DCF and STECF exports use for a confidential value, matched case-sensitively
# (see EU_DCF_Catches.csv).
CONFIDENTIALITY_SENTINEL = "C"

# The DuckDB types that get a `distinct_count`. Numeric and temporal columns get none.
_CATEGORICAL_TYPES = {"VARCHAR", "BOOLEAN"}


class QualityError(ValueError):
    """Raised when `describe_quality` is given a dataset that is not a table in the cube."""


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _year_block(cube: Cube, table: str, year_col: str) -> Optional[dict[str, Any]]:
    """Return the first year, last year and missing years of `year_col`, or None if no value casts to an integer."""
    qyear = _quote(year_col)
    lo, hi, present = cube.con.execute(
        f"SELECT MIN(y), MAX(y), list(DISTINCT y) FROM "
        f"(SELECT TRY_CAST({qyear} AS BIGINT) AS y FROM {table}) t"
    ).fetchone()
    if lo is None:
        return None
    present_years = {int(y) for y in present if y is not None}
    missing = sorted(set(range(int(lo), int(hi) + 1)) - present_years)
    return {"first": int(lo), "last": int(hi), "missing": missing}


def describe_quality(cube: Cube, dataset: str) -> dict[str, Any]:
    """Return the row count, the years and one quality entry per column for the cube dataset `dataset`.

    It raises QualityError if `dataset` is not a cube table, so callers first convert a catalogue id
    with `tools.common.resolve_cube_dataset`.
    """
    if dataset not in cube.datasets():
        raise QualityError(f"unknown dataset {dataset!r}")
    table = _quote(dataset)
    row_count = int(cube.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    columns: list[dict[str, Any]] = []
    year_col = None
    for col in cube.columns(dataset):
        name, dtype = col["name"], col["type"]
        if year_col is None and name.strip().lower() == "year":
            year_col = name
        qname = _quote(name)
        non_null = int(cube.con.execute(
            f"SELECT COUNT({qname}) FROM {table}"
        ).fetchone()[0])
        null_fraction = 0.0 if row_count == 0 else round(
            (row_count - non_null) / row_count, 4)
        sentinel_count = int(cube.con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE CAST({qname} AS VARCHAR) = ?",
            [CONFIDENTIALITY_SENTINEL],
        ).fetchone()[0])
        entry: dict[str, Any] = {
            "name": name,
            "type": dtype,
            "non_null_count": non_null,
            "null_fraction": null_fraction,
            "confidentiality_sentinel_count": sentinel_count,
        }
        # R-exported files write a missing number as the text NA, which loads the column as text
        # and makes COUNT see every row as present. NA is also Namibia's country code, so it is
        # subtracted only from a column whose other values are all numbers.
        if dtype.upper() in _CATEGORICAL_TYPES:
            na_text = int(cube.con.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {qname} = 'NA'").fetchone()[0])
            if na_text:
                other_text = int(cube.con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {qname} IS NOT NULL "
                    f"AND {qname} <> 'NA' AND TRY_CAST({qname} AS DOUBLE) IS NULL"
                ).fetchone()[0])
                numeric_with_na = other_text == 0
                entry["na_text_count"] = na_text
                entry["na_counted_as_missing"] = numeric_with_na
                if numeric_with_na:
                    entry["non_null_count"] = non_null - na_text
                    entry["null_fraction"] = 0.0 if row_count == 0 else round(
                        (row_count - entry["non_null_count"]) / row_count, 4)
        if dtype.upper() in _CATEGORICAL_TYPES:
            entry["distinct_count"] = int(cube.con.execute(
                f"SELECT COUNT(DISTINCT {qname}) FROM {table}"
            ).fetchone()[0])
        columns.append(entry)

    years = _year_block(cube, table, year_col) if year_col is not None else None

    return {
        "dataset": dataset,
        "row_count": row_count,
        "years": years,
        "columns": columns,
    }
