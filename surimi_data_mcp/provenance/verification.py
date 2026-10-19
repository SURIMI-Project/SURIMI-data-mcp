"""Checks a receipt's signature and, on request, runs its calculation again.

The MCP `verify` tool and `POST /audit/verify` both call `verify_and_recompute()`. The signature is
checked first, and the server never runs parameters taken from a receipt whose signature fails. A
recompute that gives different rows is reported with both sides, not as an error.

Receipts from the model server (`model_*` tool ids, and the older `nc_*` ids) are verified here with
the shared secret and are never recomputed, because no recompute path exists for them on either
server. They report `recompute_supported=false` and `verification_method="signature_only"`. A
signature shows that the receipt is unchanged since it was issued, which is less than showing that
the values were derived again, so the two are reported apart.

Claimed rows and recomputed rows both go through `receipts._coerce` before they are compared. The
claimed rows have been through the receipt JSON, where a `Decimal` became a string. The recomputed
rows are native Python objects, for example the `Decimal` that a DuckDB `SUM` over a `DECIMAL`
column returns.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Optional

from surimi_data_mcp.provenance.receipts import _coerce, verify_receipt

logger = logging.getLogger(__name__)


class RecomputeRejected(ValueError):
    """Raised when a policy check refuses a recompute, as opposed to an engine failure.

    The message is safe to show on the public route.
    """


def _assert_select_only(sql: str) -> None:
    """Raise `RecomputeRejected` unless `sql` is a single read-only SELECT.

    The check guards the re-execution of SQL stored in a receipt on a public route. It parses with
    sqlglot and accepts `SELECT`, `WITH ... SELECT` and `UNION`, `EXCEPT` and `INTERSECT`. Other
    statements and SQL that does not parse are rejected, and `read_only_refusal` then rejects more than
    one statement or a write inside the tree.
    """
    import sqlglot
    from sqlglot import exp

    try:
        statements = sqlglot.parse(sql, error_level=sqlglot.ErrorLevel.RAISE)
    except sqlglot.errors.ParseError as e:
        raise RecomputeRejected(
            "only SELECT statements may be recomputed (SQL did not parse)") from e
    # sqlglot.parse("") returns [None]; treat empty/blank as invalid.
    if not statements or any(st is None for st in statements):
        raise RecomputeRejected("only SELECT statements may be recomputed (empty SQL)")
    for st in statements:
        if not isinstance(st, (exp.Select, exp.SetOperation)):
            raise RecomputeRejected(
                f"only SELECT statements may be recomputed, got {type(st).__name__}")
    # The check `query_data` uses: one statement, and no write anywhere in its tree. A
    # data-modifying CTE parses as a Select that holds a Delete.
    from surimi_data_mcp.engine.sql_tools import read_only_refusal
    if read_only_refusal(sql):
        raise RecomputeRejected("only a single read-only SELECT may be recomputed")


def _rows_of(output_value: Any) -> Any:
    """The `rows` of an output value when it is a dict that has them, otherwise the value itself."""
    if isinstance(output_value, dict) and "rows" in output_value:
        return output_value["rows"]
    return output_value


def _canonical_sort(rows: Any) -> Any:
    """Sort rows by their canonical JSON so the comparison ignores row order.

    A query without `ORDER BY` returns rows in any order, so the original run and the recompute can
    agree on every value and differ in order. Row order is not part of what the receipt claims.
    """
    if not isinstance(rows, list):
        return rows
    return sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))


def _diff_rows(claimed, recomputed, cap: int = 5) -> list[dict[str, Any]]:
    """Return up to `cap` positions where the two row lists differ, as `{index, claimed, recomputed}`."""
    out = []
    for i in range(max(len(claimed or []), len(recomputed or []))):
        c = claimed[i] if i < len(claimed or []) else None
        r = recomputed[i] if i < len(recomputed or []) else None
        if c != r:
            out.append({"index": i, "claimed": c, "recomputed": r})
            if len(out) >= cap:
                break
    return out


def _recompute_aggregate(receipt: dict, cube_factory: Callable[[], Any]) -> Any:
    from surimi_data_mcp.engine import aggregate as agg_mod
    params = dict(receipt.get("input_params") or {})
    # The `input_params` keys are written by `aggregate()` when it issues the receipt, and
    # must stay valid keyword arguments of `aggregate()`.
    dataset = params.pop("dataset")
    fresh = agg_mod.aggregate(cube_factory(), dataset, **params)
    return _rows_of(fresh["value"])


def _recompute_query_data(receipt: dict, client_factory: Callable[[], Any]) -> Any:
    params = receipt.get("input_params") or {}
    _assert_select_only(params.get("sql", ""))
    fresh = client_factory().execute(params.get("sql", ""),
                                     limit=params.get("limit", 500))
    return _rows_of(fresh)


def verify_and_recompute(
    receipt: dict,
    *,
    recompute: bool = False,
    cube_factory: Optional[Callable[[], Any]] = None,
    query_client_factory: Optional[Callable[[], Any]] = None,
    secret: Optional[bytes] = None,
) -> dict[str, Any]:
    """Check a receipt's signature and, when `recompute` is true and supported, run it again.

    The result has `verified`, `reason`, `recompute_supported` and `verification_method`. A recompute adds
    `claimed`, `recomputed`, `match` and, on a mismatch, `mismatches`. Recompute is supported for
    `aggregate` with a `cube_factory` and for `query_data` with a `query_client_factory`. A receipt that
    fails the signature check is never recomputed, because its `input_params` cannot be trusted.
    """
    secret = secret or os.environ.get("SURIMI_RECEIPT_SECRET", "").encode("utf-8")
    # `verification_method` says what this server can do, not how one check came out,
    # so every return path reports it.
    if not secret:
        return {"verified": False, "reason": "secret_not_configured",
                "recompute_supported": False,
                "verification_method": "signature_only"}
    sig = verify_receipt(receipt, secret=secret)
    if not sig.get("verified"):
        return {**sig, "recompute_supported": False,
                "verification_method": "signature_only"}

    tool_id = receipt.get("tool_id", "")
    supported = ((tool_id == "aggregate" and cube_factory is not None)
                 or (tool_id == "query_data" and query_client_factory is not None))
    out: dict[str, Any] = {**sig, "recompute_supported": supported}
    if not (recompute and supported):
        # Say how the receipt was checked. Nothing recomputes `model_*` receipts on either
        # server, so for those a signature is all that stands behind the number.
        out["verification_method"] = "signature_only"
        if not supported:
            out["recompute_note"] = (
                "Verified by signature only: this receipt records a computation "
                "no recompute path exists for, so the values were not re-derived. "
                "The signature shows the receipt is unaltered since it was issued."
            )
        return out

    # Claimed rows are already _coerce-d (they came through the receipt JSON);
    # re-applying is an idempotent no-op that keeps both sides symmetrical.
    claimed = _canonical_sort(_coerce(_rows_of(receipt.get("output_value"))))
    try:
        if tool_id == "aggregate":
            recomputed = _recompute_aggregate(receipt, cube_factory)
        else:
            recomputed = _recompute_query_data(receipt, query_client_factory)
        recomputed = _canonical_sort(_coerce(recomputed))
    except Exception as e:                     # dataset gone, schema drift, ...
        # Public route: never echo driver/engine exception text (it leaks
        # schema names, hosts, SQL fragments). Log the details server-side;
        # only the policy-gate rejection keeps its (safe, intentional) message.
        logger.warning("recompute failed for receipt %s",
                       receipt.get("receipt_id"), exc_info=True)
        public = (f"recompute failed: {e}" if isinstance(e, RecomputeRejected)
                  else "recompute failed (see server logs)")
        return {**out, "verification_method": "signature_only",
                "recompute_note": "A recompute was attempted and did not complete; "
                                  "the signature still stands.",
                "match": False, "claimed": claimed, "recomputed": None,
                "mismatches": [{"error": public}]}
    match = claimed == recomputed
    result = {**out, "verification_method": "signature_and_recompute",
              "match": match, "claimed": claimed, "recomputed": recomputed}
    if not match:
        result["mismatches"] = _diff_rows(claimed or [], recomputed or [])
    return result
