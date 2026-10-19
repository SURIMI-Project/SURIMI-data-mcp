"""Experiment 1 of the surfacing loop: register the ICES stock assessment.

Four expert questions (Q-E7-2, Q-E7-3, Q-E7-4, Q-E1-5) are answered by
references/SURIMI-DATA-LAKE/4_Stock Assessment/4.1_ICES.STOCK.DATA, and no
served tool reached it. The raw-grounding swarm also lost Q-E1-5 once to this
file's literal "NA" strings, which the loader did not treat as missing.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from surimi_data_mcp.datasets import catalog
from surimi_data_mcp import server
from surimi_data_mcp.loader import load_csv
from surimi_data_mcp import sources


def _ds(i):
    return next(d for d in catalog.DATASETS if d["id"] == i)


def test_na_is_a_missing_value_sentinel_like_c():
    # 20,528 of 21,077 yield_ssb rows are the literal string "NA". Without
    # this, every ICES measure column infers TEXT and no comparison works.
    assert "na" in load_csv._NONFINITE
    t = load_csv._ColTypeInference()
    for v in ("NA", "3288", "NA", "14376"):
        t.update(v)
    assert t.could_int


def test_both_ices_tables_are_registered_and_loaded():
    assert load_csv.CSV_TO_TABLE["hive_ices_stock_assessment_latest.csv"] == "ices.stock_assessment_latest"
    assert load_csv.CSV_TO_TABLE["hive_ices_stock_assessment_timeseries.csv"] == "ices.stock_assessment_timeseries"
    for i in ("ices-stock-assessment-latest", "ices-stock-assessment-timeseries"):
        d = _ds(i)
        assert d["kind"] == "observed" and d["domain"] == "ices"


def test_the_reference_points_the_questions_hinge_on_are_declared():
    cols = {c["name"] for c in _ds("ices-stock-assessment-latest")["columns"]}
    for c in ("fish_stock", "year", "purpose", "stock_size", "stock_size_description",
              "low_stock_size", "high_stock_size", "blim", "bpa", "msy_b_trigger",
              "fishing_pressure", "fmsy", "flim", "fpa"):
        assert c in cols, c


def test_the_warning_names_the_na_and_purpose_traps():
    w = _ds("ices-stock-assessment-latest")["not_a_substitute_for"].lower()
    assert "stock_size_description" in w      # SSB lives here, not yield_ssb
    assert "purpose" in w                      # Advice vs Unofficial rows
    assert "latest" in w                       # one assessment per stock, but a time series per row


def test_uncertainty_declaration_no_longer_disowns_ices_bounds():
    # Registering ICES makes "bounds belong to tables this server does NOT
    # serve" false. A stale declaration is a refusal steer.
    txt = " ".join(open(sources.__file__, encoding="utf-8").read().split())
    assert "belong to tables this server does NOT serve" not in txt
    assert "low_stock_size" in txt and "high_stock_size" in txt


def test_no_duplicate_dataset_ids():
    ids = [d["id"] for d in catalog.DATASETS]
    assert len(ids) == len(set(ids))


def test_the_warning_quantifies_the_filter_sensitivity():
    """The first draft said the depleted count moves "by one or two". Measured
    against the loaded table it moves 24 to 32 depending on whether you require
    stock_size_description='SSB' and purpose='Advice'. A warning that understates
    a trap is worse than none: it tells the model the choice barely matters."""
    w = _ds("ices-stock-assessment-latest")["not_a_substitute_for"]
    assert "one or two" not in w
    for n in ("24", "32", "151", "193"):
        assert n in w, n


def test_the_uncertainty_count_is_not_self_contradicting():
    """The declaration said "Exactly 1 ... carries any uncertainty column" and
    then, two sentences later, named ICES as an exception carrying four. Muse
    read the first half and refused Q-E10-3 with "only one of the 28 served
    datasets". A declaration that contradicts itself steers on its first
    sentence, so the count has to be right."""
    txt = " ".join(open(sources.__file__, encoding="utf-8").read().split())
    assert "Exactly 1 of the" not in txt
    assert "low_stock_size" in txt and "cv_uncertainty" in txt
