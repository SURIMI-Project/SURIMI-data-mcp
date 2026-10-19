from surimi_data_mcp.datasets.catalog import find_dataset, list_columns

VIEW_IDS = [
    "v-employment-by-gender",
    "v-employment-by-gender-year",
    "v-employment-by-nationality",
    "v-fte-by-gender",
    "v-sales-by-species-year",
    "v-subsidies-by-country-year",
]


def test_employment_view_registered():
    d = find_dataset("v-employment-by-gender")
    assert d is not None
    assert d["table"] == "v_employment_by_gender"


def test_sales_view_columns_listed():
    out = list_columns("v-sales-by-species-year")
    names = {c["name"] if isinstance(c, dict) else c for c in out["columns"]}
    assert {"year", "species_code", "value_meur"} <= names


def test_all_six_views_resolve_and_have_bare_table_names():
    expected_tables = {
        "v-employment-by-gender": "v_employment_by_gender",
        "v-employment-by-gender-year": "v_employment_by_gender_year",
        "v-employment-by-nationality": "v_employment_by_nationality",
        "v-fte-by-gender": "v_fte_by_gender",
        "v-sales-by-species-year": "v_sales_by_species_year",
        "v-subsidies-by-country-year": "v_subsidies_by_country_year",
    }
    for view_id, table in expected_tables.items():
        d = find_dataset(view_id)
        assert d is not None, view_id
        assert d["table"] == table, view_id
        # must be a bare cube view name, not a dotted Trino FQN
        assert "." not in d["table"], view_id


def test_all_six_views_have_time_column_key():
    # list_columns / catalog code accesses ds["time_column"] unconditionally,
    # so every entry (even without a year column) must define the key.
    for view_id in VIEW_IDS:
        out = list_columns(view_id)
        assert out["error"] is None
        assert "time_column" in out


def test_views_with_year_column_declare_time_column():
    for view_id in ("v-employment-by-gender-year", "v-sales-by-species-year",
                     "v-subsidies-by-country-year"):
        d = find_dataset(view_id)
        assert d["time_column"] == "year"


def test_views_without_year_column_have_no_time_column():
    for view_id in ("v-employment-by-gender", "v-employment-by-nationality",
                     "v-fte-by-gender"):
        d = find_dataset(view_id)
        assert d["time_column"] is None


def test_employment_by_gender_year_columns():
    out = list_columns("v-employment-by-gender-year")
    names = {c["name"] for c in out["columns"]}
    assert names == {"year", "gender", "employment_persons"}


def test_employment_by_nationality_columns():
    out = list_columns("v-employment-by-nationality")
    names = {c["name"] for c in out["columns"]}
    assert names == {"nationality", "employment_persons"}


def test_fte_by_gender_columns():
    out = list_columns("v-fte-by-gender")
    names = {c["name"] for c in out["columns"]}
    assert names == {"gender", "fte"}


def test_subsidies_by_country_year_columns():
    out = list_columns("v-subsidies-by-country-year")
    names = {c["name"] for c in out["columns"]}
    assert names == {
        "year", "country_code", "direct_transfers_meur",
        "cost_reducing_transfers_meur", "general_services_meur",
        "total_support_meur",
    }
