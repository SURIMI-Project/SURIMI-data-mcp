"""'C' is a withheld value, not a zero, and 'confidential' is data.

From EU_DCF_Catches.csv's own README, which gives the semantics of the
placeholder: "confidential data are not shown, the
placeholder C is used instead". A separate `confidential` column records which
fields were withheld, as 'A' all, 'N' none, 'W' weight, 'V' value.

Three distinct non-numbers live in those measure columns and they do not mean
the same thing:

    C    withheld by the Member State  -> NULL
    NA   not applicable, 51% of the discards column -> NULL
    0    a real measured zero          -> 0

Turning C into 0 would understate every national sum, which is the whole reason
the file was kept out of the analytic set. Turning it into NULL is right, and
then `aggregate` has to report how many rows it skipped: see
tests/test_aggregate_suppressed_rows.py.
"""
from __future__ import annotations

from surimi_data_mcp.loader.load_csv import _NONFINITE


def test_the_confidential_placeholder_is_a_missing_value():
    assert "c" in _NONFINITE


def test_not_applicable_is_a_missing_value():
    assert "na" in _NONFINITE


def test_a_real_zero_is_not_a_missing_value():
    """The distinction the whole exclusion was about."""
    assert "0" not in _NONFINITE
    assert "0.0" not in _NONFINITE


def test_the_flag_letters_are_not_treated_as_missing():
    """'A', 'N', 'V', 'W' are values OF the confidential column, not sentinels.
    Mapping them to NULL would erase the record of what was suppressed."""
    for flag in ("a", "n", "v", "w"):
        assert flag not in _NONFINITE, f"{flag!r} is a confidential flag, not a sentinel"
