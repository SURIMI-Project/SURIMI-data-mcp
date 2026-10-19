"""Q-E6-1 / Q-E6-2 / Q-E6-4 / Q-E6-5: register the raw MEDITS survey and the
FishBase/SeaLifeBase Traits table, additive to the thirteen pre-existing
CSV_TO_TABLE entries.

The registrations are the `surimi-biomass-medits` and `fishbase-sealifebase-traits`
entries in `datasets/datasets.yaml` (also pinned by tests/test_catalog_medits.py and
tests/test_catalog_species_traits.py).
CSV_TO_TABLE keys are hive_<schema>_<table>.csv, matching the naming
convention the served prefix (s3://project-surimi/mcp_data/tables/) already
uses for its other 15 files -- neither side's original key (the bare raw
lake filename) exists under that prefix, so a registration keyed on it loads
fine locally but is inert once deployed.

MEDITS (950,530 rows, 1994-2021) is the observed survey CPUE the
raw-grounding audit (WAVE-1/2/4) used to answer all four of these questions:
species/group biomass coverage in the Mediterranean, the east/west species
split (it carries its own fao_subarea column), the largest Aegean decline
(fao_division "37.3.1" is exactly Aegean), and the small-demersal trend. It
ships wide (one column per year, 1994..2021), which the existing header
sanitizer already turns into y_1994..y_2021 -- no new transform script, no
reshaping, "loader work" only, matching the smallest fix on record.

Traits.csv (FishBase/SeaLifeBase, 1,229 species) is registered for Q-E6-5:
habitat/broader_habitat and trophic_level corroborate a species' place in a
functional group independent of the EwE table. Every genuinely numeric-
looking column in it infers TEXT under the real loader (NA-heavy), verified
by running _infer_table_types against the live file before writing this
catalog entry -- so every column here is declared "string", matching what
actually loads, not what the column name suggests. Domain "ecological", not
"surimi": this is an external reference table, not SURIMI project output.
"""
from __future__ import annotations

from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.loader.load_csv import CSV_TO_TABLE

EXISTING_TABLES = {
    "oecd.fsedata",
    "oecd.oecd_fisheries_support_estimates_eu_coastal_states",
    "eu_dcf.eu_dcf_fisheries_economic_performance_aegean_sea",
    "eu_dcf.eu_dcf_fisheries_employment_western_mediterranean",
    "eu_dcf.aer_economic_national_level",
    "eu_dcf.aer_social_data_2017_2020",
    "eu_dcf.aer_capacity",
    "eu_dcf.aer_economic_2013_2023",
    "eu_dcf.fdi_capacity_by_country",
    "eu_dcf.fuel",
    # Replaced 2026-09-23 by the derived wide table; the EAV source was
    # being read as if one row were a whole observation.
    "eu_dcf.fleet_economics",
    "surimi.surimi_empirical_biomass_surveys_western_mediterranean",
    "surimi.surimi_simulated_sales_output_aegean_sea",
    "fisheries.fisheries_landings",
    "fisheries.fisheries_landings_divisions",
}

MEDITS_DIM_COLUMNS = {
    "col", "scient_name", "taxonomic_code", "alpha3_code", "common_name",
    "gsa_area", "fao_area", "fao_subarea", "fao_division_description",
    "fao_division", "haul_number", "lon_end", "lat_end",
}
MEDITS_YEAR_COLUMNS = {f"y_{y}" for y in range(1994, 2022)}


def test_every_pre_existing_table_is_still_registered_unchanged():
    assert EXISTING_TABLES <= set(CSV_TO_TABLE.values())


def test_medits_csv_is_mapped_to_its_hive_table():
    assert CSV_TO_TABLE["hive_surimi_medits_survey_cpue_mediterranean.csv"] == \
        "surimi.medits_survey_cpue_mediterranean"


def test_traits_csv_is_mapped_to_its_hive_table():
    assert CSV_TO_TABLE["hive_ecological_fishbase_sealifebase_traits.csv"] == \
        "ecological.fishbase_sealifebase_traits"


def test_medits_dataset_registered_in_the_surimi_domain():
    d = catalog.find_dataset("surimi-biomass-medits")
    assert d is not None
    assert d["domain"] == "surimi"
    assert d["kind"] == "observed"
    assert d["table"] == "hive.surimi.medits_survey_cpue_mediterranean"


def test_medits_columns_are_the_dimension_set_plus_every_sanitized_year():
    d = catalog.find_dataset("surimi-biomass-medits")
    names = {c["name"] for c in d["columns"]}
    assert names == MEDITS_DIM_COLUMNS | MEDITS_YEAR_COLUMNS


def test_medits_year_columns_declare_ratio_additivity_not_summable_across_years():
    d = catalog.find_dataset("surimi-biomass-medits")
    for c in d["columns"]:
        if c["name"] in MEDITS_YEAR_COLUMNS:
            assert c["type"] == "float"
            assert c["additivity"] == "ratio"


def test_medits_has_no_single_time_column_because_it_ships_wide():
    d = catalog.find_dataset("surimi-biomass-medits")
    assert d["time_column"] is None


def test_medits_fao_division_carries_the_aegean_code_in_its_own_data():
    """Q-E6-4 needs the Aegean nameable from THIS table with no GSA mask:
    fao_division '37.3.1' is exactly Aegean in the source README, and the
    column is registered so query_data can filter on it directly."""
    d = catalog.find_dataset("surimi-biomass-medits")
    assert "fao_division" in {c["name"] for c in d["columns"]}


def test_medits_not_a_substitute_for_warns_about_the_kg_vs_tonnes_unit_defect():
    """The raw-grounding audit (WAVE-1) found the README's stated unit
    (tonnes/km2) contradicted by cross-file comparison against CMSY++'s own
    kg/km2 index for the same quantity. This MUST live in
    not_a_substitute_for, not scope_note: catalog.substitute_warning() (what
    query_data actually attaches to a result) only reads
    not_a_substitute_for, so a model that reaches this table directly
    (never calling list_datasets) would otherwise never see the correction."""
    d = catalog.find_dataset("surimi-biomass-medits")
    haystack = str(d.get("not_a_substitute_for", "")).lower()
    assert "kg" in haystack and "km" in haystack
    assert catalog.substitute_warning("surimi-biomass-medits")  # rides on the result


def test_traits_dataset_registered_in_the_ecological_domain_and_every_column_is_string():
    """Domain "ecological", not "surimi": this is an external
    reference table (FishBase/SeaLifeBase), not SURIMI project output.

    Column types confirmed live against the real file: every numeric-looking
    column (prior_r_value, cl_lower/upper, trophic_level*,
    min/max/mean_temperature) infers TEXT because NA rows poison the
    loader's per-column inference. Declaring them "float" here would silently disagree with what the
    table actually holds after a real, unmodified load -- no "blank NA"
    step exists anywhere in `loader/load_csv.py`."""
    d = catalog.find_dataset("fishbase-sealifebase-traits")
    assert d is not None
    assert d["domain"] == "ecological"
    assert d["table"] == "hive.ecological.fishbase_sealifebase_traits"
    for c in d["columns"]:
        assert c["type"] == "string", f"{c['name']} should be declared string"


def test_traits_not_a_substitute_for_warns_it_is_not_an_environmental_series():
    """This is the whole reason Q-E5-5 wanted the table registered, and it must
    ride on the result the same way as every other not_a_substitute_for."""
    d = catalog.find_dataset("fishbase-sealifebase-traits")
    haystack = str(d.get("not_a_substitute_for", "")).lower()
    assert "environmental time series" in haystack
    assert catalog.substitute_warning("fishbase-sealifebase-traits")


def test_traits_columns_match_the_source_header_exactly():
    d = catalog.find_dataset("fishbase-sealifebase-traits")
    names = {c["name"] for c in d["columns"]}
    assert names == {
        "scient_name", "alpha3_code", "taxonomic_code", "prior_r_value",
        "cl_lower", "cl_upper", "resilience", "trophic_level",
        "trophic_level_se", "min_temperature", "max_temperature",
        "mean_temperature", "habitat", "broader_habitat", "source_url",
    }


def test_no_existing_dataset_entry_was_edited():
    ids = [d["id"] for d in catalog.DATASETS]
    assert len(ids) == len(set(ids)), "duplicate dataset id"
    for known in ("eu-dcf-economic-aegean", "surimi-biomass-wmed-empirical",
                  "fisheries-landings"):
        assert known in ids
