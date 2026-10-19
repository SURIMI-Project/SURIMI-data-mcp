"""Tests for the with_receipt decorator that wraps any tool function
to automatically produce HMAC-signed receipts alongside the output."""
from __future__ import annotations

import os

import pytest

from surimi_data_mcp.provenance.receipts import verify_receipt
from surimi_data_mcp.provenance.signed_tool import with_receipt, ToolError


SECRET = b"unit-test-secret"


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", SECRET.decode())


# ---------- basic wrapping ----------

def test_wraps_function_returns_value_and_receipt():
    @with_receipt("test_tool")
    def add(a: int, b: int) -> int:
        return a + b

    out = add(2, 3)
    assert "value" in out
    assert "receipt" in out
    assert out["value"] == 5


def test_receipt_is_verifiable_with_same_secret():
    @with_receipt("test_tool")
    def add(a: int, b: int) -> int:
        return a + b

    out = add(2, 3)
    result = verify_receipt(out["receipt"], secret=SECRET)
    assert result["verified"] is True


def test_receipt_records_tool_id():
    @with_receipt("my_special_tool")
    def f() -> str:
        return "hello"

    out = f()
    assert out["receipt"]["tool_id"] == "my_special_tool"


def test_receipt_records_input_params():
    @with_receipt("test_tool")
    def f(x: int, y: int = 10) -> int:
        return x + y

    out = f(5, y=20)
    assert out["receipt"]["input_params"] == {"x": 5, "y": 20}


def test_receipt_records_output_value():
    @with_receipt("test_tool")
    def f(a: int) -> dict:
        return {"doubled": a * 2}

    out = f(7)
    assert out["receipt"]["output_value"] == {"doubled": 14}


# ---------- determinism ----------

def test_different_inputs_produce_different_signatures():
    @with_receipt("test_tool")
    def f(a: int) -> int:
        return a * 2

    s1 = f(1)["receipt"]["signature"]
    s2 = f(2)["receipt"]["signature"]
    assert s1 != s2


def test_same_inputs_at_different_times_produce_different_signatures():
    """Timestamp uniqueness should make signatures differ between calls."""
    import time

    @with_receipt("test_tool")
    def f(a: int) -> int:
        return a * 2

    s1 = f(1)["receipt"]["signature"]
    time.sleep(1.1)
    s2 = f(1)["receipt"]["signature"]
    assert s1 != s2


# ---------- error handling ----------

def test_propagates_function_errors():
    @with_receipt("test_tool")
    def f() -> int:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        f()


def test_raises_tool_error_when_secret_missing(monkeypatch):
    monkeypatch.delenv("SURIMI_RECEIPT_SECRET", raising=False)

    @with_receipt("test_tool")
    def f() -> int:
        return 1

    with pytest.raises(ToolError):
        f()


# ---------- provenance ----------

def test_extracts_provenance_from_return_dict():
    """If the wrapped function returns a dict with a 'provenance' key,
    the decorator should hoist it into the receipt."""
    @with_receipt("test_tool")
    def f() -> dict:
        return {"result": 42, "provenance": {"source": "test"}}

    out = f()
    assert out["receipt"]["provenance"] == {"source": "test"}


def test_provenance_in_receipt_affects_signature():
    @with_receipt("test_tool")
    def f(p: str) -> dict:
        return {"result": 1, "provenance": {"x": p}}

    s1 = f("a")["receipt"]["signature"]
    s2 = f("b")["receipt"]["signature"]
    assert s1 != s2


# ---------- query_data human_check ----------

class _FakeTrinoClient:
    """Minimal stand-in for the database client (mirrors tests/test_sql_tools.py)."""

    def execute(self, sql: str, limit: int = 500) -> dict:
        return {
            "rows": [{"n": 1}],
            "columns": ["n"],
            "row_count": 1,
            "error": None,
        }


def test_query_data_receipt_has_human_check():
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed

    qd = make_query_data_signed(lambda: _FakeTrinoClient(),
                                backend_id="trino://test")
    out = qd("SELECT COUNT(*) FROM t")
    hc = out["receipt"]["provenance"]["human_check"]
    assert hc["sql"] == "SELECT COUNT(*) FROM t"   # verbatim: already literal
    assert hc["engine"] == "trino://test"
    assert hc["cube_sha256"] is None               # Trino path: no cube
    assert hc["source"] is None                    # no per-table CSV lineage
