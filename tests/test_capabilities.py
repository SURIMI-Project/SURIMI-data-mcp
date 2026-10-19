"""Tests for the capability registry and its drift check."""
import pytest


def test_capability_registry_not_empty():
    from surimi_data_mcp import server
    from surimi_data_mcp.capabilities import all_capabilities
    caps = all_capabilities()
    assert len(caps) >= 1


def test_describe_returns_tool_inventory():
    from surimi_data_mcp.capabilities import describe
    result = describe(["list_datasets", "list_columns", "query_data"])
    assert "data_sources" in result
    assert "tool_inventory" in result
    assert result["tool_inventory"]["total"] == 3


def test_drift_check_no_unclaimed():
    from surimi_data_mcp import capabilities
    old = capabilities._REGISTRY[:]
    capabilities._REGISTRY.clear()
    try:
        capabilities.register(capabilities.DataSourceCapability(
            source_id="test-source",
            title="Test",
            domain="test",
            description="test",
            coverage={},
            tools=("tool_a", "tool_b"),
        ))
        result = capabilities.drift_check({"tool_a", "tool_b"})
        assert result["missing_from_registry"] == []
        assert result["missing_from_fastmcp"] == []
    finally:
        capabilities._REGISTRY[:] = old


def test_drift_check_detects_unclaimed_tool():
    from surimi_data_mcp.capabilities import register, DataSourceCapability, drift_check
    register(DataSourceCapability(
        source_id="test-source2",
        title="Test2",
        domain="test",
        description="test",
        coverage={},
        tools=("tool_x",),
    ))
    result = drift_check({"tool_x", "tool_y"})
    assert "tool_y" in result["missing_from_registry"]


def test_drift_check_detects_missing_tool():
    from surimi_data_mcp.capabilities import register, DataSourceCapability, drift_check
    register(DataSourceCapability(
        source_id="test-source3",
        title="Test3",
        domain="test",
        description="test",
        coverage={},
        tools=("tool_p", "tool_q"),
    ))
    result = drift_check({"tool_p"})
    assert "tool_q" in result["missing_from_fastmcp"]
