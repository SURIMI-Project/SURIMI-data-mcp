from surimi_data_mcp import capabilities


def test_meta_tool_classifies_as_meta():
    out = capabilities.describe(["describe_data_capabilities"])
    layers = out["tool_inventory"]["layers"]
    assert "describe_data_capabilities" in layers.get("meta", [])
