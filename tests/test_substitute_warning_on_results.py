"""The warning has to ride on the RESULT, not on a capability declaration.

Measured by the model-mcp lane on 2026-09-18: adding cannot_answer entries that
name the substitution explicitly moved fabrication 36.4% -> 36.4%. Nothing.

Why it failed, from the transcripts of the turns that kept fabricating:

    Q-E7-3   11 tool calls, receipts from query_data, query_data
    Q-E7-4    9 tool calls, receipts from query_data, query_data, query_data

Both answered a stock-assessment question by calling query_data against the
survey biomass table on THIS server. They never called the model server, so they
never read the declaration written there. A capability declaration is fetched
once, early, or not at all; it does not fire at the moment of temptation. The
model has a plausible table in hand, its columns look right, the receipt signs
cleanly, and nothing in the tool RESULT says the table is the wrong instrument.

So the text sits on the payload of the call that returns the substitute.
"""
from __future__ import annotations

from surimi_data_mcp.datasets import catalog


def test_the_simulated_sales_table_declares_what_it_is_not_a_substitute_for():
    ds = catalog.find_dataset("surimi-sales-aegean-simulated")
    warn = ds.get("not_a_substitute_for", "")
    assert warn, "the sharpest substitution case must carry the warning"
    low = warn.lower()
    assert "catch" in low, "it must name catch, the thing it gets mistaken for"
    assert "sales" in low or "sold" in low


def test_the_survey_biomass_table_declares_what_it_is_not_a_substitute_for():
    """Named by the model-mcp lane: it is answered as a stock assessment."""
    ds = catalog.find_dataset("surimi-biomass-wmed-empirical")
    warn = ds.get("not_a_substitute_for", "")
    assert warn, "this is the table the other lane measured being misused"
    low = warn.lower()
    assert "stock assessment" in low
    assert "cv_uncertainty" in low, "the cv trap has to be named where it is read"


def test_an_ordinary_table_carries_no_warning():
    """A warning on everything is noise and gets skimmed past."""
    assert not catalog.find_dataset("aer-economic-2013-2023").get("not_a_substitute_for")


def test_the_warning_is_reachable_by_dataset_id():
    assert catalog.substitute_warning("surimi-sales-aegean-simulated")
    assert catalog.substitute_warning("aer-economic-2013-2023") == ""
    assert catalog.substitute_warning("no-such-dataset") == ""


def test_the_warning_resolves_through_a_table_name_too():
    """query_data is given a Trino FQN, not a catalog id, so the lookup must
    work from the table the caller actually named."""
    assert catalog.substitute_warning(
        "hive.surimi.surimi_simulated_sales_output_aegean_sea")


# ── the parameter-spelling trap ─────────────────────────────────────────────
# aggregate took `dataset` while seven other tools take `dataset_id`. An LLM that
# guesses wrong gets a validation error and gives up; this session guessed wrong
# on its own server with full repo access.

def test_aggregate_accepts_both_spellings_of_its_dataset_argument():
    import asyncio, os
    from surimi_data_mcp import server
    os.environ.setdefault("SURIMI_RECEIPT_SECRET", "t")

    async def params():
        for t in await server.mcp.list_tools():
            if t.name == "aggregate":
                return set((getattr(t, "parameters", {}) or {}).get("properties", {}))
        return set()

    p = asyncio.run(params())
    assert "dataset" in p
    assert "dataset_id" in p, (
        "seven tools take dataset_id; aggregate taking only dataset is the "
        "third-spelling trap that makes a model give up after one error")


def test_sql_naming_the_simulated_table_is_detected():
    ids = catalog.tables_in_sql(
        "SELECT year, quantity_kt FROM hive.surimi.surimi_simulated_sales_output_aegean_sea")
    assert "surimi-sales-aegean-simulated" in ids


def test_sql_naming_an_ordinary_table_triggers_no_warning():
    # Was eu_dcf.rev_grp until 2026-09-23. Its replacement, fleet_economics,
    # carries a not_a_substitute_for note about which income measure an answer
    # used, so it is no longer an ordinary table. aer_capacity has no note.
    ids = catalog.tables_in_sql("SELECT * FROM hive.eu_dcf.aer_capacity LIMIT 1")
    assert all(not catalog.substitute_warning(i) for i in ids)


def test_the_cube_style_underscored_name_is_detected_too():
    """The cube spells the same table with underscores."""
    ids = catalog.tables_in_sql("SELECT * FROM hive_surimi_surimi_simulated_sales_output_aegean_sea")
    assert "surimi-sales-aegean-simulated" in ids


# ── a warning that induces a false absence is worse than no warning ─────────
# Measured on 1.1.4: Q-E2-1a HEEDED the simulated-sales warning, did not answer
# from that table, and then over-generalised the warning's own wording into "the
# only catch data available is a 2013-2017 hindcast". The warning said "Real
# catch OUTPUT exists only as a HINDCAST..."; the answer dropped "output".
#
# fisheries-landings is registered and serves observed landings: 31,079 Belgian
# rows, 1904-2022, 121 species, verified live. So the user was told data does
# not exist that this same server holds. correct_abstain -> abstain_wrong_reason.
#
# This is a distinct failure mode from ignoring a warning, and a worse one: the
# text was believed. A guard that is trusted has to be true at the scope a
# reader will take it, not only at the scope its author meant.

def test_the_sales_warning_does_not_imply_no_catch_data_is_served():
    warn = catalog.substitute_warning("surimi-sales-aegean-simulated")
    low = warn.lower()
    assert "landings" in low, (
        "the warning must name the observed landings that ARE served, or a "
        "reader concludes from it that no catch-family data exists")
    assert "only as a hindcast" not in low, (
        "'only' scopes over all catch data to a reader; it was meant to scope "
        "over model output alone")


def test_the_sales_warning_still_refuses_the_projection():
    """Fixing the over-reach must not lose the thing the warning is for."""
    low = catalog.substitute_warning("surimi-sales-aegean-simulated").lower()
    assert "not catch" in low
    assert "projection" in low
