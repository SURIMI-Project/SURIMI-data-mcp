"""The aggregate tool's JSON schema must say what a measure is.

`measures: list[dict]` published every measure as an untyped object, so a
client that reads the schema (as Claude Code does) could not know the keys
were `col` and `fn`. On the traced benchmark run Sonnet sent
{"column", "agg"} and 'group_by', failed three times on Q-E9-1 and gave up;
the same slip cost a probe of the live server on 2026-09-25.
"""
from pydantic import TypeAdapter
import typing

from surimi_data_mcp import server
from surimi_data_mcp.tools import figures


def _measure_schema():
    hint = typing.get_type_hints(figures.aggregate)["measures"]
    return TypeAdapter(hint).json_schema()


def test_a_measure_declares_col_and_fn():
    s = _measure_schema()
    item = s["$defs"][s["items"]["$ref"].split("/")[-1]] if "$ref" in s["items"] else s["items"]
    assert set(item["properties"]) >= {"col", "fn"}
    assert set(item["required"]) >= {"col", "fn"}


def test_fn_is_limited_to_the_engine_functions():
    s = _measure_schema()
    item = s["$defs"][s["items"]["$ref"].split("/")[-1]] if "$ref" in s["items"] else s["items"]
    assert set(item["properties"]["fn"]["enum"]) == {"sum", "avg", "count", "min", "max", "count_distinct"}


def test_the_engine_accepts_every_fn_the_schema_offers():
    from surimi_data_mcp.engine import aggregate
    s = _measure_schema()
    item = s["$defs"][s["items"]["$ref"].split("/")[-1]] if "$ref" in s["items"] else s["items"]
    assert set(item["properties"]["fn"]["enum"]) <= set(aggregate._AGG_FNS)
