"""Builds the `human_check` block of a receipt: SQL a reader can paste, and the source lineage.

The engine still runs the query with bind parameters. This module inlines the values only so that a
reader can paste the exact query into `query_data` or a SQL console. The aggregate validators have
already checked and typed the values before they reach this module.
"""
from __future__ import annotations

import math
from typing import Any, Sequence

from surimi_data_mcp.provenance import lineage

ENGINE_ID = "duckdb-cube"


def _literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and not math.isfinite(v):
        raise ValueError(f"non-finite float {v!r} cannot be inlined as SQL")
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"


def inline_sql(sql: str, params: Sequence[Any]) -> str:
    """Return `sql` with each `?` outside a string literal replaced by the matching value in `params`.

    Raises `ValueError` when the number of placeholders and the number of parameters differ.
    """
    out: list[str] = []
    it = iter(params)
    used = 0
    in_str = False
    for ch in sql:
        if ch == "'":
            in_str = not in_str
            out.append(ch)
        elif ch == "?" and not in_str:
            try:
                out.append(_literal(next(it)))
            except StopIteration:
                raise ValueError("more placeholders than params")
            used += 1
        else:
            out.append(ch)
    if used != len(params):
        raise ValueError(f"{len(params)} params but {used} placeholders")
    return "".join(out)


def build_human_check(sql: str, params: Sequence[Any], dataset: str,
                      explanation: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the block stored in the receipt's `provenance`, which is therefore signed.

    It holds the inlined `sql`, the `engine` id, the `cube_sha256`, the `source` entry for `dataset` and,
    when given, the `explanation` from `provenance.explain`.
    """
    block: dict[str, Any] = {
        "sql": inline_sql(sql, params),
        "engine": ENGINE_ID,
        "cube_sha256": lineage.get_cube_sha256(),
        "source": lineage.get_source_for(dataset),
    }
    if explanation is not None:
        block["explanation"] = explanation
    return block
