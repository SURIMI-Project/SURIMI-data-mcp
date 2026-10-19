from surimi_data_mcp.datasets import catalog

def test_aer_capacity_stock_columns():
    cols = catalog.stock_columns("aer-capacity")
    assert cols == {"number_of_vessels", "total_vessel_power", "total_vessel_tonnage"}

def test_flow_and_ratio_not_stock():
    # revenue is flow, profit_margin is ratio -> neither is a stock column
    assert "landings_value" not in catalog.stock_columns("eu-dcf-fleet-economics")
    assert "profit_margin" not in catalog.stock_columns("eu-dcf-economic-aegean")

def test_unknown_dataset_returns_empty():
    assert catalog.stock_columns("does-not-exist") == set()

def test_every_numeric_measure_is_tagged():
    # guard against forgetting a tag: every float/int non-time, non-id column
    # on a non-EAV dataset must declare additivity.
    EAV = {"aer-economic-national-level", "aer-economic-2013-2023", "aer-social-data"}
    for ds in catalog.DATASETS:
        if ds["id"] in EAV:
            continue
        for c in ds["columns"]:
            if c["type"] in ("int", "float") and c["name"] != ds["time_column"]:
                assert "additivity" in c, f"{ds['id']}.{c['name']} missing additivity"
                assert c["additivity"] in ("flow", "stock", "ratio")

EXPECTED_ADDITIVITY = {
    "eu-dcf-economic-aegean": {"gross_revenue_meur": "flow", "operating_cost_meur": "flow",
        "gross_value_added_meur": "flow", "profit_margin": "ratio", "employment_fte": "stock"},
    "eu-dcf-employment-wmed": {"employment_fte": "stock", "gender_balance_ratio": "ratio"},
    "oecd-fisheries-support-eu": {"direct_transfers_meur": "flow",
        "cost_reducing_transfers_meur": "flow", "general_services_meur": "flow"},
    "surimi-biomass-wmed-empirical": {"biomass_kt": "stock", "cv_uncertainty": "ratio"},
    "surimi-sales-aegean-simulated": {"quantity_kt": "flow", "price_eur_per_kg": "ratio",
        "value_meur": "flow"},
    "aer-capacity": {"number_of_vessels": "stock", "total_vessel_power": "stock",
        "total_vessel_tonnage": "stock", "mean_age_of_vessels": "ratio",
        "mean_loa_of_vessels": "ratio"},
    "fdi-capacity-by-country": {"total_vessels": "stock", "total_kw": "stock", "total_gt": "stock",
        "total_trips": "flow", "average_age": "ratio", "maximum_sea_days": "ratio"},
    "eu-dcf-fuel": {"sum_revenue": "flow", "sum_ecost": "flow", "fuel_efficiency": "ratio"},
    "eu-dcf-fleet-economics": {
        "landings_value": "flow", "other_income": "flow",
        "rights_income": "flow", "operating_subsidies": "flow",
        "energy_cost": "flow", "repair_cost": "flow",
        "other_variable_cost": "flow", "other_nonvariable_cost": "flow",
        "rights_cost": "flow", "personnel_cost": "flow",
        "unpaid_labour_value": "flow", "depreciation": "flow",
        "total_income": "flow", "gva": "flow", "gross_profit": "flow",
        "gross_profit_excl_subsidies_and_rights": "flow", "net_profit": "flow",
    },
}

def test_measure_additivity_is_exact():
    for ds_id, expected in EXPECTED_ADDITIVITY.items():
        ds = catalog.find_dataset(ds_id)
        assert ds is not None, ds_id
        got = {c["name"]: c.get("additivity")
               for c in ds["columns"] if c["name"] in expected}
        assert got == expected, f"{ds_id}: {got} != {expected}"


def test_fdi_capacity_registers_its_geography():
    """The FDI capacity table carries where a fleet fished, and the catalog
    dropped it.

    A reviewer asked "can we talk about average distance travelled by boat?".
    There is no distance variable anywhere, but this table's sub_region and
    fao_area_description support the nearest honest answer: trips by sea area.
    Ten of its seventeen columns were registered, and the seven left out
    included all of the geography and average_length.
    """
    from surimi_data_mcp.datasets import catalog
    cols = {c["name"] for c in catalog.find_dataset("fdi-capacity-by-country")["columns"]}
    for needed in ("sub_region", "fao_area_description", "supra_region",
                   "geo_indicator", "average_length"):
        assert needed in cols, f"{needed} not registered"


def test_fdi_capacity_average_length_is_not_a_distance():
    """average_length is vessel length overall, and reads like a journey.

    Naming it without its unit invites exactly the misreading the distance
    question would produce.
    """
    from surimi_data_mcp.datasets import catalog
    col = next(c for c in catalog.find_dataset("fdi-capacity-by-country")["columns"]
               if c["name"] == "average_length")
    assert "vessel" in (col.get("description") or "").lower()
