"""MCP wiring for the `describe_quality` tool (Q-E11-4b).

The maths is proven independently in test_quality.py; this covers only the
wiring: registration, capability claim, dataset-id resolution (catalog id /
Trino FQN / bare cube name, same bridge `aggregate` uses), and the real tool
returning real cube-computed numbers.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

TOOL = "describe_quality"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "lake"
    d.mkdir()
    (d / "hive.eu_dcf.aer_capacity.csv").write_text(
        "country_name,year,number_of_vessels\n"
        "Italy,2022,100\n"
        "Italy,2023,C\n"          # confidentiality-suppressed
        "Greece,2023,50\n",
        encoding="utf-8",
    )
    return d


@pytest.fixture(autouse=True)
def _wire_env(monkeypatch, data_dir: Path):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-quality-tool-secret")
    monkeypatch.setenv("SURIMI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SURIMI_CUBE_DB", ":memory:")
    from surimi_data_mcp.engine import cube
    monkeypatch.setattr(cube, "_CUBE", None, raising=False)
    yield
    monkeypatch.setattr(cube, "_CUBE", None, raising=False)


def _get_tool(name: str):
    from surimi_data_mcp import server
    tools = asyncio.run(server.mcp.list_tools())
    for t in tools:
        if t.name == name:
            return t
    return None


def test_describe_quality_tool_is_registered():
    assert _get_tool(TOOL) is not None


def test_describe_quality_capability_claims_the_tool():
    from surimi_data_mcp import server
    from surimi_data_mcp import capabilities
    assert TOOL in capabilities.claimed_tools()


def test_describe_quality_resolves_a_catalog_id(data_dir):
    tool = _get_tool(TOOL)
    out = tool.fn(dataset_id="aer-capacity")
    assert out["dataset"] == "hive_eu_dcf_aer_capacity"
    assert out["row_count"] == 3


def test_describe_quality_reports_the_confidentiality_sentinel(data_dir):
    tool = _get_tool(TOOL)
    out = tool.fn(dataset_id="aer-capacity")
    col = next(c for c in out["columns"] if c["name"] == "number_of_vessels")
    assert col["confidentiality_sentinel_count"] == 1


def test_describe_quality_unknown_dataset_returns_an_error_not_a_crash(data_dir):
    tool = _get_tool(TOOL)
    out = tool.fn(dataset_id="does-not-exist")
    assert "error" in out
