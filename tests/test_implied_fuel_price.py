"""Q-E1-9a: "An increase in diesel costs in Greece, how will this impact local
fishing over the next 10 years?" SURIMI can show the observed 2021-2022 shock
(the data is already registered as aer-economic-national-level), but nothing
divides energy cost by litres, so no tool expressed the implied fuel price.

Smallest fix: pivot the EAV aer-economic-national-level table into a semantic
view exposing totenercost/totenercons/totlandginc as real columns, then add
`implied_fuel_price` (totenercost / totenercons, EUR/litre) and
`energy_cost_share` (totenercost / totlandginc) metrics over it. Also flag the
Greek rows in eu-dcf-fuel (broken: zero revenue 2008-2012) and the Greek 2013/
2015/2017 AER rows (reporting-gap outliers) so a trend tool does not fit a
line through them -- via not_a_substitute_for, which rides on the tool RESULT.
"""
from __future__ import annotations

import csv as _csv
from pathlib import Path

import pytest

from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.datasets import metrics
from surimi_data_mcp.datasets.metrics import MetricError, resolve, run_query_metric, validate_registry

AER_NATIONAL = "aer_economic_national_level"

SECRET = b"test-implied-fuel-price-secret-do-not-use"


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", SECRET.decode())


def _w(path: Path, rows: list[list]) -> None:
    with open(path, "w", newline="") as f:
        _csv.writer(f).writerows(rows)


@pytest.fixture
def con(tmp_path: Path):
    from surimi_data_mcp.engine.cube import build_cube

    root = tmp_path / "lake"
    root.mkdir()
    _w(root / f"{AER_NATIONAL}.csv", [
        ["year", "country_code", "country_name", "variable_group", "variable_name",
         "variable_code", "value", "unit"],
        [2021, "GRC", "Greece", "Expenditure", "Energy costs", "totenercost", "52370066", "euro"],
        [2021, "GRC", "Greece", "Effort", "Energy consumption", "totenercons", "69356835", "litre"],
        [2021, "GRC", "Greece", "Income", "Landings income", "totlandginc", "280000000", "euro"],
        [2022, "GRC", "Greece", "Expenditure", "Energy costs", "totenercost", "67432284", "euro"],
        [2022, "GRC", "Greece", "Effort", "Energy consumption", "totenercons", "65413706", "litre"],
        [2022, "GRC", "Greece", "Income", "Landings income", "totlandginc", "253000000", "euro"],
        # A row with an unrelated variable_code that must not leak into the pivot.
        [2022, "GRC", "Greece", "Effort", "Fishing days", "totfishdays", "1390197", "day"],
        # Spain, a different country, must not contaminate Greece's row.
        [2022, "ESP", "Spain", "Expenditure", "Energy costs", "totenercost", "1", "euro"],
        [2022, "ESP", "Spain", "Effort", "Energy consumption", "totenercons", "2", "litre"],
    ])
    return build_cube(str(root)).con


# ── semantic view ────────────────────────────────────────────────────────────

def test_fuel_price_view_is_registered_in_semantic_views():
    from surimi_data_mcp.datasets.semantic_views import SEMANTIC_VIEWS
    names = {v["name"] for v in SEMANTIC_VIEWS}
    assert "v_fuel_price_by_country_year" in names


def test_fuel_price_view_pivots_the_eav_rows(con):
    from surimi_data_mcp.datasets.semantic_views import apply_semantic_views
    made = apply_semantic_views(con)
    assert "v_fuel_price_by_country_year" in made
    row = con.execute(
        "SELECT totenercost, totenercons, totlandginc FROM v_fuel_price_by_country_year "
        "WHERE year=2022 AND country_code='GRC'"
    ).fetchone()
    assert row == (67432284.0, 65413706.0, 253000000.0)


def test_fuel_price_view_does_not_leak_other_variable_codes(con):
    """totfishdays must not land in any of the three pivoted columns."""
    from surimi_data_mcp.datasets.semantic_views import apply_semantic_views
    apply_semantic_views(con)
    cols = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='v_fuel_price_by_country_year'").fetchall()}
    assert cols == {"year", "country_code", "country_name",
                     "totenercost", "totenercons", "totlandginc",
                     "implied_fuel_price", "energy_cost_share"}


def test_fuel_price_view_keeps_countries_separate(con):
    from surimi_data_mcp.datasets.semantic_views import apply_semantic_views
    apply_semantic_views(con)
    row = con.execute(
        "SELECT totenercost, totenercons FROM v_fuel_price_by_country_year "
        "WHERE year=2022 AND country_code='ESP'"
    ).fetchone()
    assert row == (1.0, 2.0)


# ── catalog registration ────────────────────────────────────────────────────

def test_fuel_price_view_registered_in_catalog():
    ds = catalog.find_dataset("v-fuel-price-by-country-year")
    assert ds is not None
    assert ds["table"] == "v_fuel_price_by_country_year"
    names = {c["name"] for c in ds["columns"]}
    assert {"year", "country_code", "totenercost", "totenercons", "totlandginc"} <= names


# ── metrics ──────────────────────────────────────────────────────────────────

def test_implied_fuel_price_metric_registered():
    m = resolve("implied_fuel_price")
    assert m.dataset == "v-fuel-price-by-country-year"
    assert m.additivity == "ratio"
    assert m.unit == "EUR/litre"


def test_energy_cost_share_metric_registered():
    m = resolve("energy_cost_share")
    assert m.dataset == "v-fuel-price-by-country-year"
    assert m.additivity == "ratio"


def test_registry_validates_against_a_real_cube(con):
    """Only the two new metrics -- the rest of REGISTRY needs datasets this
    minimal fixture does not carry."""
    from surimi_data_mcp.engine import cube as cube_module
    from surimi_data_mcp.datasets.semantic_views import apply_semantic_views
    apply_semantic_views(con)
    new_metrics = tuple(m for m in metrics.REGISTRY
                        if m.name in ("implied_fuel_price", "energy_cost_share"))
    assert len(new_metrics) == 2
    validate_registry(new_metrics, cube_module.Cube(con))


def test_implied_fuel_price_computes_the_2022_greek_shock(con):
    from surimi_data_mcp.engine import cube as cube_module
    from surimi_data_mcp.datasets.semantic_views import apply_semantic_views
    apply_semantic_views(con)
    cube = cube_module.Cube(con)
    out = run_query_metric(
        cube, "implied_fuel_price",
        where=[{"col": "country_code", "op": "=", "value": "GRC"},
               {"col": "year", "op": "=", "value": 2022}],
    )
    v = out["value"]
    assert v["rows"][0]["implied_fuel_price_avg"] == pytest.approx(67432284 / 65413706, rel=1e-6)
    assert v["unit"] == "EUR/litre"


def test_implied_fuel_price_shows_the_2021_to_2022_shock(con):
    """The 21->22 shock the certified answer states: 0.755 -> 1.031 EUR/L, a
    36.5% rise -- reproduced from the same rows a query_metric call returns."""
    from surimi_data_mcp.engine import cube as cube_module
    from surimi_data_mcp.datasets.semantic_views import apply_semantic_views
    apply_semantic_views(con)
    cube = cube_module.Cube(con)
    out = run_query_metric(
        cube, "implied_fuel_price", by=["year"],
        where=[{"col": "country_code", "op": "=", "value": "GRC"}],
    )
    by_year = {r["year"]: r["implied_fuel_price_avg"] for r in out["value"]["rows"]}
    assert by_year[2021] == pytest.approx(0.755, abs=0.001)
    assert by_year[2022] == pytest.approx(1.031, abs=0.001)


def test_energy_cost_share_computes_correctly(con):
    from surimi_data_mcp.engine import cube as cube_module
    from surimi_data_mcp.datasets.semantic_views import apply_semantic_views
    apply_semantic_views(con)
    cube = cube_module.Cube(con)
    out = run_query_metric(
        cube, "energy_cost_share",
        where=[{"col": "country_code", "op": "=", "value": "GRC"},
               {"col": "year", "op": "=", "value": 2021}],
    )
    v = out["value"]
    assert v["rows"][0]["energy_cost_share_avg"] == pytest.approx(52370066 / 280000000, rel=1e-6)


# ── query_metric surfaces not_a_substitute_for on the RESULT ────────────────

def test_query_metric_surfaces_not_a_substitute_for_from_catalog(con):
    """BRIEF rule: a capability declaration is not a guard -- the warning must
    ride on the tool RESULT. Before this fix, run_query_metric never called
    catalog.substitute_warning, so a model calling query_metric never saw a
    dataset's not_a_substitute_for text (aggregate already surfaced it)."""
    from surimi_data_mcp.engine import cube as cube_module
    from surimi_data_mcp.datasets.semantic_views import apply_semantic_views
    apply_semantic_views(con)
    cube = cube_module.Cube(con)
    out = run_query_metric(
        cube, "implied_fuel_price",
        where=[{"col": "country_code", "op": "=", "value": "GRC"},
               {"col": "year", "op": "=", "value": 2013}],
    )
    warn = catalog.substitute_warning("v-fuel-price-by-country-year")
    if warn:
        assert warn in (out["value"].get("not_a_substitute_for") or "")


# ── catalog flags: reporting gaps ride on the result too ───────────────────

def test_eu_dcf_fuel_flags_greece_as_unusable():
    warn = catalog.substitute_warning("eu-dcf-fuel")
    assert warn, "eu-dcf-fuel must flag that Greek rows are unusable"
    assert "reece" in warn or "GRC" in warn


def test_aer_national_level_flags_greek_reporting_gap_years():
    warn = catalog.substitute_warning("aer-economic-national-level")
    assert warn, "aer-economic-national-level must flag the Greek reporting gaps"
    for year in ("2013", "2015", "2017"):
        assert year in warn


def test_fuel_price_view_also_flags_the_reporting_gap():
    """The same reporting-gap flag must ride on a query_metric result too --
    implied_fuel_price/energy_cost_share go through v-fuel-price-by-country-year,
    not aer-economic-national-level directly, so the warning has to be
    carried on the derived view's own catalog entry as well."""
    warn = catalog.substitute_warning("v-fuel-price-by-country-year")
    assert warn
    for year in ("2013", "2015", "2017"):
        assert year in warn


def test_query_metric_for_energy_cost_share_carries_the_reporting_gap_flag(con):
    from surimi_data_mcp.engine import cube as cube_module
    from surimi_data_mcp.datasets.semantic_views import apply_semantic_views
    apply_semantic_views(con)
    cube = cube_module.Cube(con)
    out = run_query_metric(
        cube, "energy_cost_share",
        where=[{"col": "country_code", "op": "=", "value": "GRC"},
               {"col": "year", "op": "=", "value": 2021}],
    )
    assert "2013" in out["value"].get("not_a_substitute_for", "")
