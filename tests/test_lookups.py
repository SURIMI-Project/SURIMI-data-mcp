"""Tests for the lookup tools and their capability registration."""
import pytest


def test_list_datasets_returns_catalog():
    from surimi_data_mcp.datasets.catalog import list_datasets
    result = list_datasets()
    assert len(result["datasets"]) >= 5
    assert all("id" in d and "name" in d and "table" in d for d in result["datasets"])


def test_list_datasets_filter_by_domain():
    from surimi_data_mcp.datasets.catalog import list_datasets
    result = list_datasets(domain="oecd")
    assert len(result["datasets"]) >= 1
    assert all("oecd" in d["table"].lower() or "fse" in d["table"].lower() for d in result["datasets"])


def test_list_datasets_filter_unknown_domain_empty():
    from surimi_data_mcp.datasets.catalog import list_datasets
    result = list_datasets(domain="nonexistent_domain_xyz")
    assert len(result["datasets"]) == 0


def test_list_columns_returns_schema():
    from surimi_data_mcp.datasets.catalog import list_columns
    result = list_columns(dataset_id="oecd-fisheries-support-eu")
    assert result["error"] is None
    assert len(result["columns"]) > 0
    assert all("name" in c and "type" in c for c in result["columns"])


def test_list_columns_unknown_dataset_returns_error():
    from surimi_data_mcp.datasets.catalog import list_columns
    result = list_columns(dataset_id="nonexistent")
    assert result["error"] is not None


def test_list_columns_fuzzy_match():
    from surimi_data_mcp.datasets.catalog import list_columns
    result = list_columns(dataset_id="oecd_fisheries_support_estimates_eu_coastal_states")
    assert result["error"] is None
    assert len(result["columns"]) > 0


def test_find_dataset_by_kebab():
    from surimi_data_mcp.datasets.catalog import find_dataset
    ds = find_dataset("eu-dcf-economic-aegean")
    assert ds is not None
    assert ds["id"] == "eu-dcf-economic-aegean"


def test_find_dataset_by_table_name():
    # The lookup mechanism is what this covers; `fuel` is the table it finds.
    from surimi_data_mcp.datasets.catalog import find_dataset
    ds = find_dataset("fuel")
    assert ds is not None
    assert ds["id"] == "eu-dcf-fuel"


def test_find_dataset_by_fqn():
    from surimi_data_mcp.datasets.catalog import find_dataset
    ds = find_dataset("hive.eu_dcf.fuel")
    assert ds is not None


def test_find_dataset_partial_match():
    from surimi_data_mcp.datasets.catalog import find_dataset
    ds = find_dataset("biomass")
    assert ds is not None
    assert "biomass" in ds["id"]
