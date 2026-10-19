"""A sum of rows that are ALL zero is unreported data, not a measured zero.

hive.eu_dcf.rev_grp held 1,590 Italian rows for 2024 and every revenue value in
them was 0 (the table has since been replaced by `eu_dcf.fleet_economics`). Asking for Italy's 2024 revenue returns 0.0 with notes "ok" and an
HMAC receipt, so the server signs "Italy earned nothing in 2024" when the truth
is that nobody reported a figure. Five Italian years are like this: 2017, 2020,
2022, 2023, 2024. 2021 is the last with real values.

This is the same failure class the cannot_answer work addresses -- a confident
wrong answer where an honest absence belongs -- except here the number carries a
signature, which makes it worse than a refusal.

The engine must not guess which zeroes are real. It states the shape of what it
summed and lets the caller judge: rows existed, none carried a value.

"""
from __future__ import annotations

import csv as _csv
from pathlib import Path

import pytest

from surimi_data_mcp.engine.cube import build_cube
from surimi_data_mcp.engine.aggregate import aggregate


SECRET = b"test-all-zero-secret-do-not-use-in-prod"

# Mirrors the real shape: a year whose rows all carry 0, beside years that do
# not. `revenue` is the measure; `country` and `year` are the filter columns.
ROWS = [
    ["year", "country", "revenue"],
    ["2021", "ITA", "100.0"],
    ["2021", "ITA", "50.0"],
    ["2021", "ITA", "0.0"],
    ["2024", "ITA", "0.0"],
    ["2024", "ITA", "0.0"],
    ["2024", "ITA", "0.0"],
    ["2024", "ESP", "7.5"],
]


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", SECRET.decode())


@pytest.fixture
def cube(tmp_path: Path):
    root = tmp_path / "lake"
    root.mkdir()
    with open(root / "rev.csv", "w", newline="") as f:
        _csv.writer(f).writerows(ROWS)
    return build_cube(str(root))


def _agg(cube, filters):
    return aggregate(cube, "rev", measures=[{"col": "revenue", "fn": "sum"}],
                     filters=filters)


def test_an_all_zero_sum_says_no_value_was_reported(cube):
    """The Italy-2024 case. Rows exist, every one is zero."""
    out = _agg(cube, [{"col": "country", "op": "=", "value": "ITA"},
                      {"col": "year", "op": "=", "value": 2024}])
    notes = out["value"]["notes"].lower()
    assert "ok" != notes.strip(), "an all-zero sum must not be reported as 'ok'"
    assert "no" in notes and ("report" in notes or "value" in notes), notes


def test_the_all_zero_note_states_how_many_rows_it_summed(cube):
    """"3 rows, none carrying a value" is checkable; a bare zero is not."""
    out = _agg(cube, [{"col": "country", "op": "=", "value": "ITA"},
                      {"col": "year", "op": "=", "value": 2024}])
    assert "3" in out["value"]["notes"], out["value"]["notes"]


def test_the_zero_is_still_returned_and_still_signed(cube):
    """Disclosure, not refusal. The arithmetic is right; the reading is at risk.

    Suppressing the number would break every caller that sums correctly over a
    genuinely-zero slice, so the value stays and the note carries the warning.
    """
    out = _agg(cube, [{"col": "country", "op": "=", "value": "ITA"},
                      {"col": "year", "op": "=", "value": 2024}])
    assert out["value"]["rows"][0]["revenue_sum"] == 0.0
    assert out.get("receipt"), "the result must still carry a receipt"


def test_a_real_nonzero_sum_is_untouched(cube):
    """2021 has values. It must read exactly as before: notes 'ok'."""
    out = _agg(cube, [{"col": "country", "op": "=", "value": "ITA"},
                      {"col": "year", "op": "=", "value": 2021}])
    assert out["value"]["rows"][0]["revenue_sum"] == 150.0
    assert out["value"]["notes"] == "ok"


def test_a_slice_with_no_rows_at_all_is_not_confused_with_all_zero(cube):
    """No rows and all-zero rows are different absences and must read differently.

    The engine already returns an honest empty result when nothing matched. That
    path must not start claiming values were unreported.
    """
    out = _agg(cube, [{"col": "country", "op": "=", "value": "FRA"}])
    assert out["value"]["rows"] == []
    assert "unreported" not in out["value"]["notes"].lower()


def test_one_real_value_among_zeroes_is_not_flagged(cube):
    """ESP 2024 is a single non-zero row. Only ALL-zero slices are flagged."""
    out = _agg(cube, [{"col": "country", "op": "=", "value": "ESP"}])
    assert out["value"]["rows"][0]["revenue_sum"] == 7.5
    assert "unreported" not in out["value"]["notes"].lower()
