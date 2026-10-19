"""Q-E11-1: list_datasets must surface what years a dataset's data actually
covers -- not a filename or a README's claimed span, which can disagree with
each other and with the data (the EU economic table's README claims 2013-2023
while the file itself runs a wider span). The manifest's `first`/`last`/
`missing` fields (scripts/build_sources_manifest.py) are read from the data;
list_datasets must surface them as a `notes` string for the affected dataset.
"""
from __future__ import annotations

import json

import pytest

from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.provenance import lineage


@pytest.fixture(autouse=True)
def _isolate_lineage_state():
    yield
    lineage.reset()


def _write_manifest(tmp_path, data):
    p = tmp_path / "sources_manifest.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_list_datasets_carries_a_notes_field_from_the_real_data_span(tmp_path, monkeypatch):
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", _write_manifest(tmp_path, {
        "hive_eu_dcf_aer_economic_2013_2023": {
            "file": "hive_eu_dcf_aer_economic_2013_2023.csv",
            "sha256": "a" * 64, "n_rows": 355340, "agency": "STECF AER",
            "original_url": None, "first": 2008, "last": 2024, "missing": [],
        },
    }))
    lineage.reset()
    out = catalog.list_datasets(domain="eu_dcf")
    entry = next(d for d in out["datasets"] if d["id"] == "aer-economic-2013-2023")
    assert "2008" in entry["notes"] and "2024" in entry["notes"]


def test_notes_names_missing_years_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", _write_manifest(tmp_path, {
        "hive_eu_dcf_aer_economic_2013_2023": {
            "file": "x.csv", "sha256": "a" * 64, "n_rows": 1, "agency": "STECF AER",
            "original_url": None, "first": 2008, "last": 2011, "missing": [2010],
        },
    }))
    lineage.reset()
    out = catalog.list_datasets(domain="eu_dcf")
    entry = next(d for d in out["datasets"] if d["id"] == "aer-economic-2013-2023")
    assert "2010" in entry["notes"]


def test_no_notes_field_without_a_matching_manifest_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", _write_manifest(tmp_path, {}))
    lineage.reset()
    out = catalog.list_datasets(domain="eu_dcf")
    entry = next(d for d in out["datasets"] if d["id"] == "aer-economic-2013-2023")
    assert "notes" not in entry


def test_no_notes_field_when_manifest_entry_has_no_temporal_data(tmp_path, monkeypatch):
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", _write_manifest(tmp_path, {
        "hive_eu_dcf_aer_economic_2013_2023": {
            "file": "x.csv", "sha256": "a" * 64, "n_rows": 1, "agency": "STECF AER",
            "original_url": None, "first": None, "last": None, "missing": [],
        },
    }))
    lineage.reset()
    out = catalog.list_datasets(domain="eu_dcf")
    entry = next(d for d in out["datasets"] if d["id"] == "aer-economic-2013-2023")
    assert "notes" not in entry


def test_no_notes_field_when_no_manifest_is_loaded_at_all(monkeypatch, tmp_path):
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", str(tmp_path / "absent.json"))
    lineage.reset()
    out = catalog.list_datasets(domain="eu_dcf")
    entry = next(d for d in out["datasets"] if d["id"] == "aer-economic-2013-2023")
    assert "notes" not in entry
