"""HMAC-signed receipts for tool calls.

The design follows arXiv 2603.10060, "Tool Receipts, Not Zero-Knowledge Proofs". A receipt records a
tool's inputs, outputs and provenance and is signed with an HMAC keyed by a secret that only the
server holds. A verifier signs the same payload again and compares the two signatures in constant
time. The LLM cannot produce a valid signature without the secret.

Usage:
    r = issue_receipt(
        tool_id="query_data",
        input_params={"sql": "SELECT 1 AS n", "limit": 500},
        output_value={"rows": [{"n": 1}]},
        secret=os.environ["SURIMI_RECEIPT_SECRET"].encode(),
        provenance={"engine": "duckdb-cube"},
    )
    # r is JSON-serializable and is returned alongside the tool output.

    res = verify_receipt(r, secret=...)
    if res["verified"]: ...
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any


SCHEMA_VERSION = 1


class ReceiptError(ValueError):
    """Raised for an empty secret or a value that cannot go into a receipt."""


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coerce(obj: Any) -> Any:
    """Convert database types into JSON-native values, recursively.

    A `Decimal` becomes a string, a `datetime` or `date` an ISO 8601 string, and dicts, lists and tuples
    are converted item by item. Strings, numbers, booleans and None are unchanged, and any other type
    raises `ReceiptError`. `issue_receipt` runs this, so the stored receipt is JSON-serializable as well
    as the signed bytes.
    """
    if obj is None or isinstance(obj, (str, bool, int, float)):
        return obj
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _coerce(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_coerce(v) for v in obj]
    raise ReceiptError(
        f"value of type {type(obj).__name__} is not serializable for a receipt"
    )


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize the payload as the JSON that is signed: sorted keys, no whitespace, `ensure_ascii=False`.

    The payload has already been through `_coerce`, so `json.dumps` meets only JSON-native types.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sign(payload: dict[str, Any], secret: bytes) -> str:
    canonical = _canonical_bytes(payload)
    return hmac.new(secret, canonical, hashlib.sha256).hexdigest()


def issue_receipt(
    tool_id: str,
    input_params: dict[str, Any],
    output_value: Any,
    secret: bytes,
    *,
    timestamp: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a signed receipt for one tool call.

    The receipt holds `version`, `tool_id`, `input_params`, `output_value`, `timestamp`, `provenance`
    when given, and `signature`. Raises `ReceiptError` for an empty secret or a value that cannot be serialized.
    """
    if not secret:
        raise ReceiptError("secret must be non-empty bytes")

    body: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "tool_id": tool_id,
        "input_params": _coerce(input_params),
        "output_value": _coerce(output_value),
        "timestamp": timestamp or _now_iso(),
    }
    if provenance is not None:
        body["provenance"] = _coerce(provenance)

    body["signature"] = _sign(body, secret)
    return body


def verify_receipt(receipt: dict[str, Any], secret: bytes) -> dict[str, Any]:
    """Check a receipt's fields, version and signature against `secret`.

    Returns `{"verified": bool, "reason": str}`, with extra keys for a missing field or an unsupported
    version. A mismatch is reported and not raised. Only an empty secret raises `ReceiptError`.
    """
    if not secret:
        raise ReceiptError("secret must be non-empty bytes")

    required = {"version", "tool_id", "input_params", "output_value", "timestamp", "signature"}
    missing = required - set(receipt.keys())
    if missing:
        return {"verified": False, "reason": "missing_field", "missing": sorted(missing)}

    if receipt["version"] != SCHEMA_VERSION:
        return {
            "verified": False,
            "reason": "unsupported_version",
            "got": receipt["version"],
            "supported": SCHEMA_VERSION,
        }

    claimed = receipt["signature"]
    body = {k: v for k, v in receipt.items() if k != "signature"}
    expected = _sign(body, secret)

    if not hmac.compare_digest(claimed, expected):
        return {"verified": False, "reason": "signature_mismatch"}

    return {"verified": True, "reason": "ok"}
