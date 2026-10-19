"""A table whose years are COLUMNS still has a time dimension.

analyze_trend refuses on surimi-biomass-medits with "This dataset has no time
dimension", because MEDITS is wide: y_1994 through y_2021 are 28 columns, not
28 rows, and the catalogue records time_column=None. oecd-fsedata is the same
shape with y_2000..y_2022.

So every client asking "which species declined most in the Aegean" is told to
hand-roll the trend in SQL, and each one invents a different method. Muse chose
y_2002, which is zero on all 89,156 Aegean rows because the Aegean was not
surveyed that year, and hedged. A strong model chose 2001 to 2021 and ranked by
ABSOLUTE change; the grounded answer ranks by PERCENTAGE over 1994 to 2021 and
names a different species first. Neither client was wrong to try. The server
made them invent the method, so they invented different ones.

Unpivoting belongs on the server, once, where it can also skip the years a
selection has no data for.
"""
import re

import pytest

from surimi_data_mcp.datasets import wide_years


def test_it_recognises_a_run_of_year_columns():
    cols = ["alpha3_code", "gsa_area"] + [f"y_{y}" for y in range(1994, 2022)]
    years = wide_years.year_columns(cols)
    assert len(years) == 28
    assert years[0] == (1994, "y_1994") and years[-1] == (2021, "y_2021")


def test_two_year_columns_are_not_a_time_series():
    """A stray pair is a coincidence; a run is a series."""
    assert wide_years.year_columns(["y_2000", "y_2001", "name"]) == []


def test_a_table_with_a_real_year_column_is_left_alone():
    assert wide_years.year_columns(["year", "country", "value"]) == []


def test_unpivot_sql_produces_one_row_per_year():
    sql = wide_years.unpivot_sql("survey", ["alpha3_code"],
                                 [(1994, "y_1994"), (1995, "y_1995")], "cpue")
    assert sql.count("SELECT") >= 2 and "UNION ALL" in sql
    assert '1994 AS "year"' in sql and '"y_1994" AS "cpue"' in sql
    assert '"alpha3_code"' in sql


def test_the_unpivot_carries_a_where_clause_through():
    sql = wide_years.unpivot_sql("survey", ["alpha3_code"], [(1994, "y_1994")],
                                 "cpue", where='"gsa_area" IN (20, 22, 23)')
    assert sql.count('"gsa_area" IN (20, 22, 23)') == 1


def test_a_year_with_no_data_anywhere_in_the_selection_is_reported():
    """The failure that cost Q-E6-4: y_2002 is zero on every Aegean row, which
    is indistinguishable from a flat index unless someone says so."""
    rows = [{"year": 1994, "v": 3.0}, {"year": 2002, "v": 0.0},
            {"year": 2021, "v": 1.0}]
    assert wide_years.empty_years(rows, "year", "v") == [2002]


def test_all_populated_means_nothing_to_report():
    rows = [{"year": 1994, "v": 3.0}, {"year": 2021, "v": 1.0}]
    assert wide_years.empty_years(rows, "year", "v") == []


# --- wiring: the tool must stop refusing -----------------------------------

import duckdb

from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.engine import cube as cube_module
from surimi_data_mcp.tools import analyses, common
from surimi_data_mcp.datasets import wide_years as wy
from surimi_data_mcp.engine.cube import Cube


@pytest.fixture
def wide_cube(monkeypatch):
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE hive_surimi_wide AS SELECT * FROM (VALUES
            ('AAA', 20, 9.0, 0.0, 1.0),
            ('BBB', 20, 4.0, 0.0, 3.0),
            ('CCC', 22, 2.0, 0.0, 2.0)
        ) AS t(alpha3_code, gsa_area, y_1994, y_2002, y_2021)
    """)
    c = Cube(con)
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "0" * 32)
    return c


def test_a_wide_view_is_queryable_once_unpivoted(wide_cube):
    years = wy.year_columns(["alpha3_code", "gsa_area", "y_1994", "y_2002", "y_2021"])
    sql = wy.unpivot_sql("hive_surimi_wide", ["alpha3_code", "gsa_area"],
                         years, "value")
    wide_cube.con.execute(f'CREATE OR REPLACE VIEW v_wide AS {sql}')
    rows = [dict(zip([d[0] for d in wide_cube.con.description], r))
            for r in wide_cube.con.execute(
                'SELECT * FROM v_wide ORDER BY "year", alpha3_code').fetchall()]
    assert len(rows) == 9, "3 species x 3 years"
    assert {r["year"] for r in rows} == {1994, 2002, 2021}
    assert wy.empty_years(rows, "year", "value") == [2002]


def test_the_tool_no_longer_refuses_a_wide_dataset(monkeypatch, wide_cube):
    """The guard returned "this dataset has no time dimension" for MEDITS,
    which has 28 of them sitting in its column names."""
    monkeypatch.setattr(catalog, "find_dataset", lambda i: {
        "id": "surimi-biomass-medits", "table": "hive_surimi_wide",
        "time_column": None,
        "columns": [{"name": n} for n in
                    ("alpha3_code", "gsa_area", "y_1994", "y_2002", "y_2021")]})
    monkeypatch.setattr(cube_module, "get_cube", lambda: wide_cube)
    monkeypatch.setattr(common, "resolve_cube_dataset", lambda t, c: "hive_surimi_wide")

    out = analyses.analyze_trend("surimi-biomass-medits", metric="value",
                            group_by="alpha3_code")
    assert "no time dimension" not in str(out.get("error") or ""), out


def test_a_column_the_catalogue_advertises_but_the_cube_lacks_is_dropped():
    """catalog.find_dataset('surimi-biomass-medits') lists a column named
    'col' that is not in the cube table, so the unpivot failed live with
    Binder Error: Referenced column "col" not found in FROM clause. The cube
    is what can be queried; the catalogue is a description of it and can
    drift."""
    con = duckdb.connect(":memory:")
    con.execute('CREATE TABLE t AS SELECT * FROM (VALUES (1, 2.0)) AS x(gsa_area, y_1994)')
    real = wy.cube_columns(Cube(con), "t")
    assert real == ["gsa_area", "y_1994"]
    assert "col" not in real


def test_cube_columns_on_a_missing_table_is_empty_not_an_exception():
    con = duckdb.connect(":memory:")
    assert wy.cube_columns(Cube(con), "no_such_table") == []


def test_the_view_is_TEMP_because_the_deployed_cube_is_read_only(tmp_path):
    """The deployed cube is attached read-only, so CREATE VIEW is refused with
    'Cannot execute statement of type "CREATE" on database "cube" which is
    attached in read-only mode'. A TEMP view lives in the temp database and is
    allowed. Verified against a real read-only connection, not assumed."""
    db = tmp_path / "ro.duckdb"
    w = duckdb.connect(str(db))
    w.execute('CREATE TABLE t AS SELECT * FROM (VALUES (20, 9.0, 5.0, 1.0)) '
              'AS x(gsa_area, y_1994, y_1995, y_2021)')
    w.close()
    ro = duckdb.connect(str(db), read_only=True)
    years = wy.year_columns(["gsa_area", "y_1994", "y_1995", "y_2021"])
    sql = wy.unpivot_sql("t", ["gsa_area"], years, "value")
    ro.execute(wy.create_view_sql("v_t", sql))
    assert ro.execute('SELECT COUNT(*) FROM v_t').fetchone()[0] == 3


def test_create_view_sql_says_TEMP():
    assert "TEMP" in wy.create_view_sql("v", "SELECT 1")


def test_an_unpivot_with_no_years_refuses_rather_than_emitting_broken_sql():
    """It produced "CREATE OR REPLACE TEMP VIEW v AS " and a parser error."""
    with pytest.raises(ValueError):
        wy.unpivot_sql("t", ["a"], [], "value")


def test_the_measure_key_is_found_not_assumed():
    """The live rows are {"year": 1994, "alpha3_code": "WBB", "value_sum": 0.0}.
    empty_years was asked for "value", found nothing on any row, and reported
    all 28 years as having no data while the finding beside it showed real
    values. A note that is confidently wrong is worse than no note."""
    rows = [{"year": 1994, "alpha3_code": "A", "value_sum": 3.0},
            {"year": 2002, "alpha3_code": "A", "value_sum": 0.0}]
    assert wy.measure_key(rows, "year", ["alpha3_code"]) == "value_sum"


def test_no_measure_key_is_none_not_a_guess():
    assert wy.measure_key([{"year": 1994, "a": "x"}], "year", ["a"]) is None
    assert wy.measure_key([], "year", []) is None


def test_empty_years_with_the_real_row_shape():
    rows = [{"year": 1994, "value_sum": 3.0}, {"year": 2002, "value_sum": 0.0},
            {"year": 2021, "value_sum": 1.0}]
    key = wy.measure_key(rows, "year", [])
    assert wy.empty_years(rows, "year", key) == [2002]
