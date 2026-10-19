import csv as _csv
import os
import pytest
from surimi_data_mcp.paths import APP_ROOT
from surimi_data_mcp.engine.cube import build_cube
from surimi_data_mcp.datasets.semantic_views import SEMANTIC_VIEWS, apply_semantic_views

SOCIAL = "hive_eu_dcf_aer_social_data_2017_2020"
SALES = "hive_surimi_surimi_simulated_sales_output_aegean_sea"

def _w(path, rows):
    with open(path, "w", newline="") as f:
        _csv.writer(f).writerows(rows)

@pytest.fixture
def con(tmp_path):
    root = tmp_path / "lake"; root.mkdir()
    # social: socemploy male 100+10(case)=110, female 5; NULL-gender 999 excluded;
    # socfte male 40 (must NOT leak into the employment view)
    _w(root / f"{SOCIAL}.csv", [
        ["year", "gender", "nationality", "variable_code", "variable_value"],
        ["2020", "Male", "national", "socemploy", "100"],
        ["2020", "male", "national", "socemploy", "10"],
        ["2020", "Female", "national", "socemploy", "5"],
        ["2020", "", "national", "socemploy", "999"],
        ["2020", "Male", "national", "socfte", "40"],
    ])
    # sales: MAC across 2 markets sums to 59.62; PIL 40.98
    _w(root / f"{SALES}.csv", [
        ["year", "market_code", "species_code", "value_meur"],
        ["2030", "ESAQA", "MAC", "30.0"],
        ["2030", "ESARN", "MAC", "29.62"],
        ["2030", "ESAQA", "PIL", "40.98"],
    ])
    return build_cube(str(root)).con

def test_apply_creates_views_present_in_definitions(con):
    made = apply_semantic_views(con)
    assert "v_employment_by_gender" in made
    assert "v_sales_by_species_year" in made
    names = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'v_%'").fetchall()}
    assert {"v_employment_by_gender", "v_sales_by_species_year"} <= names

def test_employment_view_socemploy_only_case_merged_null_excluded(con):
    apply_semantic_views(con)
    rows = dict(con.execute(
        "SELECT gender, employment_persons FROM v_employment_by_gender ORDER BY employment_persons DESC").fetchall())
    assert rows == {"male": 110.0, "female": 5.0}   # 999 NULL-gender excluded; socfte 40 not counted

def test_sales_view_sums_across_markets(con):
    apply_semantic_views(con)
    rows = dict(con.execute(
        "SELECT species_code, value_meur FROM v_sales_by_species_year WHERE year='2030'").fetchall())
    assert rows["MAC"] == pytest.approx(59.62)   # 30.0 + 29.62, not a single 30.0 row
    assert rows["PIL"] == pytest.approx(40.98)

def test_view_has_no_variable_code_column(con):
    apply_semantic_views(con)
    cols = [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='v_employment_by_gender'").fetchall()]
    assert "variable_code" not in cols   # cross-code sum is unrepresentable


def test_partial_oecd_table_is_skipped_not_crashed(tmp_path):
    """Regression: a base table PRESENT with columns MISSING (e.g. a partial
    OECD fixture that only has [year, country_code, direct_transfers_meur]) must make apply_semantic_views
    skip that one view -- not raise and abort every other view in the loop."""
    root = tmp_path / "lake"
    root.mkdir()
    # OECD table present but missing cost_reducing_transfers_meur / general_services_meur.
    _w(root / "hive_oecd_oecd_fisheries_support_estimates_eu_coastal_states.csv", [
        ["year", "country_code", "direct_transfers_meur"],
        ["2019", "ESP", "10.0"], ["2020", "ESP", "12.0"],
    ])
    # Complete social table so the employment view CAN build.
    _w(root / f"{SOCIAL}.csv", [
        ["year", "gender", "nationality", "variable_code", "variable_value"],
        ["2020", "Male", "national", "socemploy", "100"],
        ["2020", "Female", "national", "socemploy", "5"],
    ])
    con = build_cube(str(root)).con
    made = apply_semantic_views(con)  # must not raise
    assert "v_subsidies_by_country_year" not in made
    assert "v_employment_by_gender" in made
    names = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'v_%'").fetchall()}
    assert "v_subsidies_by_country_year" not in names
    assert "v_employment_by_gender" in names


# The real CSV exports sit in data/exports/ where the data is loaded (not in a fresh clone).
EXPORTS = str(APP_ROOT / "data" / "exports")

@pytest.mark.skipif(not os.path.isdir(EXPORTS), reason="real exports not present")
def test_build_cube_bakes_views_and_reproduces_real_oracle():
    c = build_cube(EXPORTS)
    ds = set(c.datasets())
    assert "v_employment_by_gender" in ds and "v_sales_by_species_year" in ds
    emp = dict(c.con.execute(
        "SELECT gender, ROUND(employment_persons,2) FROM v_employment_by_gender").fetchall())
    assert emp["male"] == pytest.approx(239138.68, abs=0.01)
    assert emp["female"] == pytest.approx(7395.23, abs=0.01)
    assert emp["unknown"] == pytest.approx(140.99, abs=0.01)
    fte = dict(c.con.execute(
        "SELECT gender, ROUND(fte,0) FROM v_fte_by_gender").fetchall())
    assert fte["male"] == pytest.approx(176097, abs=1)
    assert fte["female"] == pytest.approx(4701, abs=1)
    nat = dict(c.con.execute(
        "SELECT nationality, ROUND(employment_persons,2) FROM v_employment_by_nationality").fetchall())
    assert nat["national"] == pytest.approx(193173.80, abs=0.5)
    sales = dict(c.con.execute(
        "SELECT species_code, value_meur FROM v_sales_by_species_year WHERE year='2030'").fetchall())
    assert sales["MAC"] == pytest.approx(59.62) and sales["PIL"] == pytest.approx(40.98) and sales["HKE"] == pytest.approx(35.63)
    sub = c.con.execute(
        "SELECT total_support_meur FROM v_subsidies_by_country_year WHERE year='2010' AND country_code='ESP'").fetchone()[0]
    assert sub == pytest.approx(218.49)
