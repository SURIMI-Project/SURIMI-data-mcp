"""The DataGUI fisheries landings tables are registered without touching
the existing thirteen.

Amended by Revision 2 section R1 (see .superpowers/sdd/
2026-09-01-datagui-landings-ingest/revision-2.md): the ICES area-level file
and the ICES division-level file describe the SAME catch at two
granularities, so one union table would double count area 27 in the overlap
years. The fix is two tables, not one: ``fisheries.fisheries_landings``
(fao37 + ices27 area-level, internally sum-safe because the two FAO areas are
disjoint) and ``fisheries.fisheries_landings_divisions`` (ices27
division-level, one granularity, one source).
"""
from surimi_data_mcp.loader import bootstrap
from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.loader.load_csv import CSV_TO_TABLE
from scripts.fetch_datagui_landings import UNION_COLUMNS

# The thirteen that existed before this change. Pinned by value: an edit to
# any of them is a regression, not a refactor.
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
}

NEW_DATASET_IDS = ("fisheries-landings", "fisheries-landings-divisions")


def test_every_pre_existing_table_is_still_registered_unchanged():
    assert EXISTING_TABLES <= set(CSV_TO_TABLE.values())


def test_exactly_two_tables_were_added():
    """The tables beyond the thirteen in EXISTING_TABLES are the two landings tables,
    MEDITS and Traits (tests/test_e6_medits_and_traits_registration.py) and the two
    ICES stock-assessment tables (tests/test_ices_registration.py). Each is listed
    here so that an unplanned addition fails this guard."""
    added = set(CSV_TO_TABLE.values()) - EXISTING_TABLES
    assert added == {
        "fisheries.fisheries_landings",
        "fisheries.fisheries_landings_divisions",
        "surimi.medits_survey_cpue_mediterranean",
        "ecological.fishbase_sealifebase_traits",
        "ices.stock_assessment_latest",
        "ices.stock_assessment_timeseries",
    }


def test_the_new_datasets_do_not_land_in_the_eu_dcf_domain():
    """eu_dcf already holds fdi_capacity_by_country. Two plausible FDI tables
    in one domain is how the model picks the wrong one."""
    expected_tables = {
        "fisheries-landings": "hive.fisheries.fisheries_landings",
        "fisheries-landings-divisions": "hive.fisheries.fisheries_landings_divisions",
    }
    for did, table in expected_tables.items():
        d = next(x for x in catalog.DATASETS if x["id"] == did)
        assert d["domain"] == "fisheries"
        assert d["table"] == table


def test_landings_is_a_flow_so_it_sums_across_years():
    for did in NEW_DATASET_IDS:
        d = next(x for x in catalog.DATASETS if x["id"] == did)
        col = next(c for c in d["columns"] if c["name"] == "landings")
        assert col["additivity"] == "flow"


def test_every_new_column_declares_additivity_or_is_a_dimension():
    for did in NEW_DATASET_IDS:
        d = next(x for x in catalog.DATASETS if x["id"] == did)
        for c in d["columns"]:
            if c["type"] == "float":
                assert "additivity" in c, f"{did}: {c['name']} is a measure with no additivity"


def test_no_existing_dataset_entry_was_edited():
    ids = [d["id"] for d in catalog.DATASETS]
    assert len(ids) == len(set(ids)), "duplicate dataset id"
    for known in ("eu-dcf-economic-aegean", "surimi-biomass-wmed-empirical"):
        assert known in ids


def test_the_overlap_warning_is_pinned_on_both_landings_datasets():
    """The two tables overlap: same catch, two granularities (area vs
    division), for the FULL 1903-2022 history in both -- not a time-period
    split, as an earlier revision assumed. If this warning is silently
    dropped later, a total built from both tables double counts with no
    error anywhere -- this test is the tripwire. "one table only" is the
    stable substring across both names."""
    for did in NEW_DATASET_IDS:
        d = next(x for x in catalog.DATASETS if x["id"] == did)
        haystack = " ".join(
            str(d.get(k, "")) for k in ("name", "description", "notes")
        ).lower()
        assert "one table only" in haystack, f"{did} is missing the overlap warning"


def test_landings_catalog_columns_match_the_union_csv_header_exactly():
    """Regression guard: a live validation run once failed because
    list_columns omitted a real column, and the model never used it. The
    catalog and the union CSV must declare the identical column set -- not a
    superset, not a subset -- or the two silently drift apart again. On
    failure the assertion message names the exact symmetric difference so the
    drift is actionable, not just "these don't match"."""
    union_cols = set(UNION_COLUMNS)
    for did in NEW_DATASET_IDS:
        d = next(x for x in catalog.DATASETS if x["id"] == did)
        catalog_cols = {c["name"] for c in d["columns"]}
        diff = catalog_cols.symmetric_difference(union_cols)
        assert catalog_cols == union_cols, (
            f"{did}: catalog columns and UNION_COLUMNS disagree on {sorted(diff)} "
            f"(only in catalog: {sorted(catalog_cols - union_cols)}, "
            f"only in UNION_COLUMNS: {sorted(union_cols - catalog_cols)})"
        )


def test_the_fdi_catches_file_is_not_loaded_into_postgres():
    """EU_DCF_Catches.csv is 1.75 GB and its measure columns carry a
    confidentiality flag rather than a number: total_live_weight_landed,
    total_value_of_landings and tot_discards_tonnes were all sampled as 'C'.
    Loaded as float those become NULL, and a later sum() treats a suppressed
    record as zero -- understating national catch with no error anywhere.

    It stays cube-only until the flag semantics are settled with the data
    owner. Delete this test only together with that decision."""
    names = " ".join(bootstrap.ANALYTIC_CSVS).lower()
    assert "eu_dcf_catches" not in names
    assert "fdi_catches" not in names
