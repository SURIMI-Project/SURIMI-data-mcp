import json

import pytest

from surimi_data_mcp.provenance import human_check
from surimi_data_mcp.provenance import lineage


@pytest.fixture(autouse=True)
def _reset_lineage():
    yield
    lineage.reset()


def test_inline_sql_strings_numbers_null():
    sql = "SELECT * FROM t WHERE a = ? AND b IN (?, ?) AND c = ? AND d = ?"
    out = human_check.inline_sql(sql, ["x'y", 1, 2.5, None, True])
    assert out == ("SELECT * FROM t WHERE a = 'x''y' AND b IN (1, 2.5) "
                   "AND c = NULL AND d = TRUE")


def test_inline_sql_param_count_mismatch_raises():
    with pytest.raises(ValueError):
        human_check.inline_sql("SELECT ?", [1, 2])


def test_inline_sql_ignores_question_mark_inside_string_literal():
    out = human_check.inline_sql("SELECT '?', ? FROM t", [5])
    assert out == "SELECT '?', 5 FROM t"


def test_inline_sql_nonfinite_float_raises():
    with pytest.raises(ValueError):
        human_check.inline_sql("SELECT ?", [float("nan")])
    with pytest.raises(ValueError):
        human_check.inline_sql("SELECT ?", [float("inf")])


def test_build_block_with_source(tmp_path, monkeypatch):
    mpath = tmp_path / "m.json"
    mpath.write_text(json.dumps({"ds1": {"file": "ds1.csv", "sha256": "ab" * 32,
                                          "n_rows": 7, "agency": "FAO"}}))
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", str(mpath))
    monkeypatch.setenv("SURIMI_SOURCE_BASE_URL", "https://mcp.example/sources")
    lineage.reset()
    blk = human_check.build_human_check("SELECT 1 FROM ds1 WHERE a=?", [3], "ds1")
    assert blk["sql"] == "SELECT 1 FROM ds1 WHERE a=3"
    assert blk["engine"] == "duckdb-cube"
    assert blk["source"]["url"].endswith("/ds1.csv")
    assert "cube_sha256" in blk          # may be None for :memory: -- key present


def test_build_block_without_manifest_source_is_null(monkeypatch, tmp_path):
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", str(tmp_path / "absent.json"))
    lineage.reset()
    blk = human_check.build_human_check("SELECT 1", [], "ds1")
    assert blk["source"] is None
