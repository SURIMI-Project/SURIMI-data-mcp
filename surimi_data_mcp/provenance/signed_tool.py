"""Decorator that makes a tool function return an HMAC-signed receipt next to its value.

Usage:
    @with_receipt("query_data")
    def query_data_signed(sql, limit=500):
        return {...}

    out = query_data_signed("SELECT 1")
    # out == {"value": {...}, "receipt": {...signed...}}

When the returned value is a dict with a `provenance` key, that entry goes into the receipt. The
secret is read from `SURIMI_RECEIPT_SECRET` at call time, so a test can set the variable per test.
"""
from __future__ import annotations

import functools
import inspect
import os
from typing import Any, Callable

from surimi_data_mcp.provenance.receipts import issue_receipt


class ToolError(RuntimeError):
    """Raised when a tool cannot be signed, for example when `SURIMI_RECEIPT_SECRET` is not set."""


def _get_secret() -> bytes:
    raw = os.environ.get("SURIMI_RECEIPT_SECRET", "")
    if not raw:
        raise ToolError(
            "SURIMI_RECEIPT_SECRET env var not set; cannot sign receipts"
        )
    return raw.encode("utf-8")


def _bind_args(fn: Callable, args: tuple, kwargs: dict) -> dict[str, Any]:
    """Return the call's arguments as a `{name: value}` dict, with defaults applied."""
    sig = inspect.signature(fn)
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


def with_receipt(tool_id: str) -> Callable:
    """Return a decorator under which every call of the function returns `{"value", "receipt"}`.

    The receipt's `input_params` are the call's arguments and its `output_value` is the function's result.
    """

    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            secret = _get_secret()
            params = _bind_args(fn, args, kwargs)
            value = fn(*args, **kwargs)

            provenance: dict[str, Any] | None = None
            if isinstance(value, dict) and "provenance" in value:
                provenance = value["provenance"]

            receipt = issue_receipt(
                tool_id=tool_id,
                input_params=params,
                output_value=value,
                secret=secret,
                provenance=provenance,
            )
            return {"value": value, "receipt": receipt}

        return wrapper

    return deco
