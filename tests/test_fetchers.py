"""Tests for the fetcher tools (Trino integration).

Tests marked `live` need a reachable Trino at 127.0.0.1:8081 with the SURIMI
hive catalog loaded. CI runs `-m "not live"`; run the full set locally against
a live stack.
"""
import pytest

TRINO_URL = "http://127.0.0.1:8081"


@pytest.fixture
def client():
    from surimi_data_mcp.engine.databases import TrinoClient
    return TrinoClient(host="127.0.0.1", port=8081, catalog="hive")


@pytest.mark.live
def test_query_data_with_sql(client):
    result = client.execute(
        "SELECT country_code, direct_transfers_meur "
        "FROM hive.oecd.oecd_fisheries_support_estimates_eu_coastal_states "
        "WHERE year = 2020 LIMIT 5"
    )
    assert result["error"] is None
    assert len(result["rows"]) <= 5
    assert len(result["rows"]) > 0
    assert "country_code" in result["columns"]


@pytest.mark.live
def test_query_data_returns_columns(client):
    result = client.execute(
        'SELECT "country.code", "country.name" FROM hive.oecd.fsedata LIMIT 1'
    )
    assert result["error"] is None
    assert "country.code" in result["columns"]
    assert "country.name" in result["columns"]


def test_query_data_bad_column_returns_error(client):
    result = client.execute(
        "SELECT nonexistent_col FROM hive.oecd.fsedata"
    )
    assert result["error"] is not None


def test_query_data_bad_table_returns_error(client):
    result = client.execute(
        "SELECT * FROM hive.oecd.no_such_table_xyz"
    )
    assert result["error"] is not None


@pytest.mark.live
def test_describe_table(client):
    result = client.describe_table("hive.oecd.fsedata")
    assert result["error"] is None
    assert len(result["columns"]) > 0
    assert any(c["name"] == "country.code" for c in result["columns"])


def test_describe_table_not_found(client):
    result = client.describe_table("hive.oecd.nonexistent_xyz")
    assert result["error"] is not None


def test_query_with_self_correction_hint(client):
    result = client.execute(
        "SELECT bad_col FROM hive.oecd.oecd_fisheries_support_estimates_eu_coastal_states"
    )
    assert result["error"] is not None
    if result.get("available_columns"):
        assert "country_code" in [c["name"] for c in result["available_columns"]]
