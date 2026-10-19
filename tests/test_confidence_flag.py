"""Q-E10-3: "What is the confidence of the data being right?"

SURIMI has no single confidence score. It has proxy measures: CI columns on the ICES
stock table, a method flag on CMSY++ and QA columns on DATRAS. The server reports, per
dataset, whether an uncertainty column exists, derived from the column list in
`datasets.yaml`, so a caller can tell "this table has no confidence measure" from "not
checked".
"""
from surimi_data_mcp.datasets import catalog


def test_the_empirical_biomass_survey_is_the_one_dataset_with_an_uncertainty_column():
    assert catalog.has_uncertainty_column("surimi-biomass-wmed-empirical") is True
    assert catalog.uncertainty_columns("surimi-biomass-wmed-empirical") == ["cv_uncertainty"]


def test_a_dataset_with_no_uncertainty_column_says_so():
    assert catalog.has_uncertainty_column("eu-dcf-economic-aegean") is False
    assert catalog.uncertainty_columns("eu-dcf-economic-aegean") == []


def test_an_unknown_dataset_id_reports_no_uncertainty_column_rather_than_raising():
    assert catalog.has_uncertainty_column("does-not-exist") is False
    assert catalog.uncertainty_columns("does-not-exist") == []


def test_exactly_three_registered_datasets_carry_an_uncertainty_column():
    """Verified 2026-09-30 against the raw lake and the served tables: the
    empirical biomass survey (cv_uncertainty) and the two ICES stock assessment
    tables (low_/high_ bounds, a confidence interval on 67 per cent of stock-size
    estimates). The earlier pin said one, because the ICES bounds were not read
    as uncertainty columns. If a fourth is added, this test is meant to force a
    look, not to silently start passing at 4."""
    with_flag = sorted(d["id"] for d in catalog.DATASETS if catalog.has_uncertainty_column(d["id"]))
    assert with_flag == ["ices-stock-assessment-latest", "ices-stock-assessment-timeseries",
                         "surimi-biomass-wmed-empirical"]


def test_list_datasets_reports_the_flag_for_every_dataset_including_the_no():
    """The whole point: a false flag must still be a visible field, not an
    absent key. The existing dict-comprehension in list_datasets drops any
    key whose value is falsy (`if v`), which is exactly the bug -- it would
    make every "No" indistinguishable from "not asked"."""
    out = catalog.list_datasets()
    assert len(out["datasets"]) > 1
    for row in out["datasets"]:
        assert "has_uncertainty_column" in row, (
            f"{row.get('id')} is missing has_uncertainty_column entirely, "
            f"so its No reads as unasked rather than as a No"
        )

    yes_row = next(d for d in out["datasets"] if d["id"] == "surimi-biomass-wmed-empirical")
    assert yes_row["has_uncertainty_column"] is True

    no_row = next(d for d in out["datasets"] if d["id"] == "eu-dcf-economic-aegean")
    assert no_row["has_uncertainty_column"] is False


def test_list_datasets_domain_filter_still_works_with_the_new_field():
    out = catalog.list_datasets(domain="oecd")
    assert len(out["datasets"]) >= 1
    for row in out["datasets"]:
        assert "has_uncertainty_column" in row


def test_the_ices_interval_bounds_count_as_uncertainty_columns():
    """ICES publishes each estimate with low_ and high_ bounds, a confidence
    interval. Neither prefix matched the name markers, so list_datasets reported
    both ICES tables as having no uncertainty column while 67 per cent of their
    stock-size estimates carry one."""
    for ds in ("ices-stock-assessment-latest", "ices-stock-assessment-timeseries"):
        assert catalog.has_uncertainty_column(ds) is True, ds
        cols = catalog.uncertainty_columns(ds)
        assert "low_stock_size" in cols and "high_stock_size" in cols, ds


def test_a_prefix_match_does_not_flag_unrelated_columns():
    """Only the bound columns match: a plain measure such as stock_size does not."""
    assert "stock_size" not in catalog.uncertainty_columns("ices-stock-assessment-latest")


def test_the_aegean_sales_table_warns_that_its_market_codes_are_spanish():
    """Verified 2026-09-30: surimi-sales-aegean-simulated is named for the Aegean
    but its market codes are Spanish (ESAQA, ESARN, ESBCN), and nothing in the
    lake ties it to a SURIMI run. A model should not present it as a Greek or
    Aegean result."""
    ds = catalog.find_dataset("surimi-sales-aegean-simulated")
    text = ds["not_a_substitute_for"]
    assert "market codes are Spanish" in text and "ESAQA" in text
