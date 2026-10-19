"""The fuel-price view never built on the deployed cube, and said nothing.

Found 2026-09-21 by rebuilding the deployed cube: 23 datasets came out and
`v_fuel_price_by_country_year` was not among them, while the other six
semantic views built. `list_datasets` therefore advertised it with
cube_servable false, and Q-E1-9a and Q-E1-9b had no implied fuel price to read.

The cause is a name that exists in one ingest path and not the other. The view
declares its base table as `aer_economic_national_level`, the raw-lake stem of
"AER_economic_national level.csv". The deployed cube is built from the exported
CSVs, where the same table arrives as
`hive_eu_dcf_aer_economic_national_level`. apply_semantic_views skips a view
whose base table is absent, by design and silently, so the mismatch cost two
questions and produced no error anywhere.

A view must therefore accept EITHER name. Skipping silently is right for a
partial fixture; it is wrong when the table is present under its other name.
"""
from __future__ import annotations

import duckdb

from surimi_data_mcp.datasets import semantic_views as sv

_COLS = ("year", "country_code", "country_name", "variable_code", "value")


def _base(con, name: str) -> None:
    con.execute(f'CREATE TABLE "{name}" (' + ", ".join(f'"{c}" VARCHAR' for c in _COLS) + ")")
    con.execute(f'INSERT INTO "{name}" VALUES '
                "('2020','ESP','Spain','totenercost','100'),"
                "('2020','ESP','Spain','totenercons','50'),"
                "('2020','ESP','Spain','totlandginc','400')")


def _fuel_view():
    return next(v for v in sv.SEMANTIC_VIEWS
                if v["name"] == "v_fuel_price_by_country_year")


def test_it_builds_from_the_raw_lake_stem():
    con = duckdb.connect(":memory:")
    _base(con, "aer_economic_national_level")
    assert "v_fuel_price_by_country_year" in sv.apply_semantic_views(con)


def test_it_also_builds_from_the_hive_exported_name():
    """The deployed cube's name. This is the case that was failing."""
    con = duckdb.connect(":memory:")
    _base(con, "hive_eu_dcf_aer_economic_national_level")
    assert "v_fuel_price_by_country_year" in sv.apply_semantic_views(con)


def test_a_genuinely_absent_base_table_is_still_skipped():
    """Silence is correct when the table is really not there."""
    con = duckdb.connect(":memory:")
    assert "v_fuel_price_by_country_year" not in sv.apply_semantic_views(con)


def test_the_view_declares_both_names():
    v = _fuel_view()
    names = set(v.get("base_tables") or [v["base_table"]])
    assert "aer_economic_national_level" in names
    assert "hive_eu_dcf_aer_economic_national_level" in names
