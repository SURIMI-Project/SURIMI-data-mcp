import hashlib
import json

import pytest

from surimi_data_mcp.provenance import lineage


@pytest.fixture(autouse=True)
def _isolate_lineage_state():
    """Keep module-level lineage caches from leaking between tests."""
    yield
    lineage.reset()


def _write_manifest(tmp_path, data):
    p = tmp_path / "sources_manifest.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_get_source_for_returns_entry_with_url(tmp_path, monkeypatch):
    mpath = _write_manifest(tmp_path, {
        "hive_eu_dcf_aer_social_data_2017_2020": {
            "file": "aer_social_data_2017_2020.csv", "sha256": "9c2e" + "0" * 60,
            "n_rows": 18948, "agency": "STECF AER", "original_url": "https://stecf.example"}
    })
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", mpath)
    monkeypatch.setenv("SURIMI_SOURCE_BASE_URL", "https://mcp.example/sources")
    lineage.reset()
    src = lineage.get_source_for("hive_eu_dcf_aer_social_data_2017_2020")
    assert src["file"] == "aer_social_data_2017_2020.csv"
    assert src["url"] == "https://mcp.example/sources/aer_social_data_2017_2020.csv"
    assert src["agency"] == "STECF AER"


def test_get_source_for_unknown_dataset_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", _write_manifest(tmp_path, {}))
    lineage.reset()
    assert lineage.get_source_for("nope") is None


def test_missing_manifest_is_honest_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", str(tmp_path / "absent.json"))
    lineage.reset()
    assert lineage.get_source_for("anything") is None
    assert lineage.manifest_loaded() is False


def test_corrupt_manifest_is_honest_empty(tmp_path, monkeypatch):
    p = tmp_path / "sources_manifest.json"
    p.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", str(p))
    lineage.reset()
    assert lineage.manifest_loaded() is False
    assert lineage.get_source_for("x") is None


def test_cube_sha256_cached(tmp_path, monkeypatch):
    cube_file = tmp_path / "cube.duckdb"
    cube_file.write_bytes(b"hello-cube")
    monkeypatch.setenv("SURIMI_CUBE_DB", str(cube_file))
    lineage.reset()
    expect = hashlib.sha256(b"hello-cube").hexdigest()
    assert lineage.get_cube_sha256() == expect
    cube_file.write_bytes(b"CHANGED")          # cached: still the old hash
    assert lineage.get_cube_sha256() == expect


def test_cube_sha256_none_for_memory_db(monkeypatch):
    monkeypatch.setenv("SURIMI_CUBE_DB", ":memory:")
    lineage.reset()
    assert lineage.get_cube_sha256() is None
