"""compare_countries and generate_report fold a wide year table too (2026-10-13).

analyze_trend learned to fold y_YYYY columns into a year series; the other two time-aware analyses
kept the old guard and told the model that oecd-fsedata, 23 year columns per country, "has no time
dimension". They now fold the table the same way.
"""
import duckdb
import pytest

from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.engine import cube as cube_module
from surimi_data_mcp.engine.cube import Cube
from surimi_data_mcp.knowledge import docsearch
from surimi_data_mcp.tools import analyses, common


@pytest.fixture
def wide_country_table(monkeypatch):
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE hive_oecd_wide AS SELECT * FROM (VALUES
            ('ESP', 1.0, 2.0, 30.0),
            ('ITA', 4.0, 5.0, 6.0)
        ) AS t(country_code, y_2000, y_2001, y_2002)
    """)
    cube = Cube(con)
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "0" * 32)
    monkeypatch.setattr(catalog, "find_dataset", lambda i: {
        "id": "oecd-fsedata", "table": "hive_oecd_wide", "time_column": None,
        "columns": [{"name": n, "type": "string"} for n in
                    ("country_code", "y_2000", "y_2001", "y_2002")]})
    monkeypatch.setattr(cube_module, "get_cube", lambda: cube)
    monkeypatch.setattr(common, "resolve_cube_dataset", lambda t, c: "hive_oecd_wide")

    class _NoDocs:
        def search(self, query, top_k=5):
            return []
    monkeypatch.setattr(docsearch, "get_index", lambda: _NoDocs())
    return cube


def test_compare_countries_ranks_a_wide_table(wide_country_table):
    out = analyses.compare_countries("oecd-fsedata", metric="y_2002", period="2002")
    assert "no time dimension" not in str(out.get("error") or ""), out
    assert not out.get("error"), out
    assert "ESP" in str(out["data"]) and "ITA" in str(out["data"])


def test_generate_report_reports_a_wide_table(wide_country_table):
    out = analyses.generate_report("oecd-fsedata", focus="support over time")
    assert "no time dimension" not in str(out.get("error") or ""), out
    assert not out.get("error"), out
    assert "key_metric" in str(out["data"])
