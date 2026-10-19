"""A number from a model run must say so, next to the number.

describe_data_capabilities declared this gap on 2026-09-16: "no field in
any tool output marks which is which. One of thirteen served tables carries any
uncertainty measure." Declaring it was not enough.

What it costs, observed on the expert benchmark 2026-09-18. Asked what the
potential catches are over the coming years -- a question no SURIMI run has
produced output for -- the assistant returned a year-by-year 2025-2040 table
carrying four HMAC receipts and a verify link. The numbers were not invented:
surimi-sales-aegean-simulated really does hold 144 rows spanning 2025-2040. But
explain_provenance on that dataset returns "No SURIMI documentation matched this
dataset" with zero sources, so nothing in the answer says a model produced it,
and the receipt attests only that the number came from the table.

A receipt is a claim about arithmetic, not about epistemic status. A user
clicking Verify gets a green check on a projection no model produced.

CLAUDE.md, repo-wide: "Keep mock and demo data visibly separate from real
backend data."
"""
from __future__ import annotations

from surimi_data_mcp.datasets import catalog


def _by_id(ds_id):
    return next((d for d in catalog.DATASETS if d["id"] == ds_id), None)


def test_every_dataset_declares_whether_it_is_observed_or_modelled():
    missing = [d["id"] for d in catalog.DATASETS if not d.get("kind")]
    assert not missing, f"datasets with no kind: {missing}"


def test_kind_is_one_of_the_two_allowed_values():
    bad = [(d["id"], d.get("kind")) for d in catalog.DATASETS
           if d.get("kind") not in ("observed", "modelled")]
    assert not bad, f"unexpected kind values: {bad}"


def test_the_simulated_sales_table_is_marked_modelled():
    """The table behind the 2025-2040 projection answer."""
    assert _by_id("surimi-sales-aegean-simulated")["kind"] == "modelled"


def test_the_view_over_the_simulated_table_is_also_modelled():
    """A view does not launder a model output into an observation."""
    assert _by_id("v-sales-by-species-year")["kind"] == "modelled"


def test_the_empirical_biomass_survey_is_marked_observed():
    assert _by_id("surimi-biomass-wmed-empirical")["kind"] == "observed"


def test_the_dcf_and_oecd_tables_are_observed():
    for ds_id in ("aer-economic-2013-2023", "oecd-fisheries-support-eu", "eu-dcf-fleet-economics"):
        assert _by_id(ds_id)["kind"] == "observed", ds_id


def test_catalog_exposes_the_modelled_set():
    """Callers need the set without walking DATASETS themselves."""
    modelled = catalog.modelled_datasets()
    assert "surimi-sales-aegean-simulated" in modelled
    assert "v-sales-by-species-year" in modelled
    assert "aer-economic-2013-2023" not in modelled


# ── the disclosure text that reaches the answer ─────────────────────────────

def test_a_modelled_dataset_produces_a_disclosure_note():
    note = catalog.modelled_note("surimi-sales-aegean-simulated")
    assert note, "a modelled dataset must produce a note"
    low = note.lower()
    assert "model" in low
    assert "not" in low and ("observ" in low or "measur" in low)


def test_an_observed_dataset_produces_no_note():
    """Silence is the correct output for a measurement; noise would be ignored."""
    assert catalog.modelled_note("aer-economic-2013-2023") == ""


def test_an_unknown_dataset_produces_no_note():
    assert catalog.modelled_note("does-not-exist") == ""


def test_the_note_does_not_claim_a_model_run_produced_it():
    """What this server knows is that the TABLE is model output.

    It does not know which run wrote it, or whether any SURIMI experiment
    produced these particular rows -- explain_provenance returns no sources for
    this dataset. Saying more than that would repeat the original error in a
    new place.
    """
    note = catalog.modelled_note("surimi-sales-aegean-simulated").lower()
    for overclaim in ("simulation run", "this run", "produced by the surimi model run"):
        assert overclaim not in note, note


# ── the declared gap must track what was actually fixed ─────────────────────

def test_the_gap_entry_no_longer_claims_output_is_unmarked():
    """Half of that gap is now closed; leaving the claim makes it a false one.

    The entry said "no field in any tool output marks which is which". aggregate
    and query_metric now return data_kind, and list_datasets carries kind, so
    repeating it would be the same error the false-abstention tests exist to
    catch.
    """
    from surimi_data_mcp import capabilities
    from surimi_data_mcp import server
    entries = [ca for cap in capabilities.all_capabilities()
               for ca in cap.cannot_answer]
    hits = [ca for ca in entries if "uncertain" in ca.question_class.lower()]
    assert hits, "the uncertainty gap should still be declared"
    detail = hits[0].detail.lower()
    assert "no field in any tool output marks which is which" not in detail
    assert "data_kind" in detail, "it should name the field that now answers it"


def test_the_uncertainty_count_is_current():
    """The dataset count in the declaration is `len(catalog.DATASETS)`, and one dataset has a column named for uncertainty."""
    from surimi_data_mcp import capabilities
    from surimi_data_mcp import server
    from surimi_data_mcp.datasets import catalog
    with_unc = [d["id"] for d in catalog.DATASETS
                if any("uncert" in c["name"].lower() or c["name"].lower().startswith("cv_")
                       for c in d["columns"])]
    assert len(with_unc) == 1, with_unc
    entries = [ca for cap in capabilities.all_capabilities()
               for ca in cap.cannot_answer if "uncertain" in ca.question_class.lower()]
    assert "thirteen" not in entries[0].detail.lower(), "stale count"
    assert str(len(catalog.DATASETS)) in entries[0].detail
