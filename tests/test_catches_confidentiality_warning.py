"""The catches dataset must declare its suppression, with real numbers.

A warning that says "some data may be confidential" tells the model nothing it
can act on. This one carries what was measured over 400,000 rows of the real
file, so the model can say how incomplete a figure is rather than hedge:

    confidential='A' (all withheld)    142,404 rows, 35.6%
    confidential='V' (value withheld)    6,321 rows,  1.6%
    confidential='N' (nothing withheld) 251,275 rows, 62.8%

and the flag predicts the placeholders exactly: 'C' appears in
total_live_weight_landed and tot_discards_tonnes 142,404 times each, and in
total_value_of_landings 148,725 times, which is 142,404 + 6,321.

This repo's own lesson is that a declaration can go unread or go stale, so the
load-bearing guard is the per-query count on the aggregate RESULT
(tests/test_aggregate_suppressed_rows.py). This warning is the second line, for
the model reading list_datasets before it queries anything.
"""
from __future__ import annotations

from surimi_data_mcp.datasets import catalog


def _entry():
    return catalog.find_dataset("eu-dcf-catches")


def test_it_warns_at_all():
    assert (_entry().get("warning") or "").strip()


def test_the_warning_carries_the_measured_share():
    w = _entry()["warning"]
    assert "35.6" in w or "142,404" in w


def test_the_warning_says_a_total_is_a_lower_bound():
    assert "lower bound" in _entry()["warning"].lower()


def test_the_warning_names_the_flag_column_and_its_letters():
    w = _entry()["warning"]
    assert "confidential" in w
    for letter in ("A", "N"):
        assert letter in w


def test_it_does_not_claim_the_placeholder_is_a_zero():
    w = _entry()["warning"].lower()
    assert "zero" in w  # it must actively say C is NOT zero
