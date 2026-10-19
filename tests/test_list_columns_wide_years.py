"""A discovery result must not read as a dead end.

Four questions on 2026-09-21 fetched exactly the right table and stopped before
computing anything. The clearest is Q-E6-4, "which fish species declined most in
the Aegean in the last 20 years": the model called list_columns on
surimi-biomass-medits, received 41 column names, and answered

    "only catalogue / schema information, not a computed decline"

while 28 of those 41 names were y_1994 through y_2021, the per-year series the
decline is computed from. It had the data and could not see that it did.

The prompt already forbids this in almost the same words ("'the data returned to
date does not include it' ... is NOT an abstention, it is an unfinished turn"),
so another instruction is not the fix. What has worked four times in this repo is
augmenting a tool the model ALREADY calls, on its RESULT: the aggregate
latest-year default, available_tables on a table-not-found, the cross-server
referral moved onto the result, the manifest lineage moved onto the result. What
has failed is asking it to switch tools: query_metric and breakdown both scored
0 calls across a 101-case run.

list_columns is called constantly, so it is the right place. A wide year series
is now reported AS a series, with the span and the call that computes over it.
"""
from __future__ import annotations

from surimi_data_mcp.datasets import catalog


def test_a_wide_year_table_reports_the_series():
    out = catalog.list_columns("surimi-biomass-medits")
    w = out["wide_years"]
    assert w["count"] == 28
    assert (w["first"], w["last"]) == (1994, 2021)
    assert w["columns"][0] == "y_1994" and w["columns"][-1] == "y_2021"


def test_the_result_names_what_to_do_next():
    """The point is that discovery stops looking terminal."""
    out = catalog.list_columns("surimi-biomass-medits")
    nxt = out["next_step"]
    assert "y_1994" in nxt and "y_2021" in nxt
    assert "query_data" in nxt or "aggregate" in nxt
    # It must say the schema alone is not an answer, in the model's own words.
    assert "not an answer" in nxt.lower() or "unfinished" in nxt.lower()


def test_a_narrow_table_says_nothing():
    """No wide series, no note. A discovery result that always shouts is noise."""
    out = catalog.list_columns("aer-capacity")
    assert not out.get("wide_years")
    assert "next_step" not in out


def test_the_column_list_is_unchanged():
    """Augmenting a result must not alter what it already returned."""
    out = catalog.list_columns("surimi-biomass-medits")
    names = [c["name"] for c in out["columns"]]
    assert len(names) == 41
    assert "scient_name" in names and "y_2000" in names


def test_a_missing_dataset_still_errors_cleanly():
    out = catalog.list_columns("no-such-dataset")
    assert out["columns"] == [] and out["error"]
    assert "wide_years" not in out
