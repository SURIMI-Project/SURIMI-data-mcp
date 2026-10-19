"""A total over confidential data is a LOWER BOUND, and must say so.

EU_DCF_Catches.csv is 1.75 GB of FDI catch data and its README settles what the
placeholders mean, which nobody had read: "confidential data are not shown, the
placeholder C is used instead". Column 24, `confidential`, records which fields
a Member State suppressed, as 'A' (all), 'N' (none), 'W' (weight) or 'V' (value).

Measured over 400,000 rows of the real file, the flag predicts the placeholders
exactly: confidential='A' appears 142,404 times and 'C' appears in
total_live_weight_landed and tot_discards_tonnes exactly 142,404 times each;
total_value_of_landings carries 'C' 148,725 times, which is 142,404 ('A') plus
6,321 ('V'). Not one row off.

So 35.6% of rows have their landed weight withheld. The loader maps C to NULL,
which is right, and DuckDB's SUM then skips those rows silently. A national
landings total computed that way omits more than a third of the data and
`aggregate` signs a receipt on it. That is the confident-wrong-number failure
the receipt system exists to prevent, and it is why `loader/bootstrap.py` keeps this file
out of the analytic set.

The count has to ride on the RESULT, not sit in a declaration. This repo has
learned that twice: a cross-server referral in a CannotAnswer declaration went
unread until it moved onto the tool result, and a stale declaration then
overrode a correct result. A number the model cannot see cannot qualify its
answer.
"""
from __future__ import annotations

import duckdb
import pytest

from surimi_data_mcp.engine import aggregate as agg_mod
from surimi_data_mcp.provenance import lineage
from surimi_data_mcp.engine.cube import Cube


@pytest.fixture()
def cube(monkeypatch, tmp_path):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", str(tmp_path / "absent.json"))
    monkeypatch.delenv("SURIMI_CUBE_DB", raising=False)
    lineage.reset()
    con = duckdb.connect(":memory:")
    # Two countries. ESP suppresses two of four rows ('A'), NLD suppresses none.
    con.execute("CREATE TABLE catches ("
                "country VARCHAR, total_live_weight_landed DOUBLE, confidential VARCHAR)")
    con.execute("INSERT INTO catches VALUES "
                "('ESP', 10.0, 'N'), ('ESP', 20.0, 'N'), "
                "('ESP', NULL, 'A'), ('ESP', NULL, 'A'), "
                "('NLD', 5.0, 'N')")
    con.execute("CREATE TABLE plain (country VARCHAR, v DOUBLE)")
    con.execute("INSERT INTO plain VALUES ('NO', 1.0), ('NO', 2.0)")
    return Cube(con)


def _agg(cube, dataset, col, **kw):
    return agg_mod.aggregate(cube, dataset, [{"col": col, "fn": "sum"}], **kw)


def test_the_suppressed_row_count_is_reported(cube):
    out = _agg(cube, "catches", "total_live_weight_landed", dimensions=["country"])
    sup = out["value"]["suppressed"]
    assert sup["total_live_weight_landed"] == 2


def test_the_note_says_the_total_is_a_lower_bound(cube):
    out = _agg(cube, "catches", "total_live_weight_landed", dimensions=["country"])
    n = out["value"]["notes"].lower()
    assert "lower bound" in n
    assert "confidential" in n


def test_the_confidential_flags_are_broken_out(cube):
    """'A' and 'V' mean different fields were withheld, so the reason is data."""
    out = _agg(cube, "catches", "total_live_weight_landed", dimensions=["country"])
    assert out["value"]["suppressed_by_flag"] == {"A": 2}


def test_the_figure_itself_is_unchanged(cube):
    """Reporting suppression must not alter the arithmetic."""
    out = _agg(cube, "catches", "total_live_weight_landed", dimensions=["country"])
    got = {r["country"]: r["total_live_weight_landed_sum"] for r in out["value"]["rows"]}
    assert got == {"ESP": 30.0, "NLD": 5.0}


def test_a_filter_narrows_the_suppressed_count_too(cube):
    """The count must describe the rows this figure was computed over."""
    out = _agg(cube, "catches", "total_live_weight_landed",
               filters=[{"col": "country", "op": "=", "value": "NLD"}])
    assert out["value"]["suppressed"]["total_live_weight_landed"] == 0
    assert "lower bound" not in out["value"]["notes"].lower()


def test_a_dataset_with_no_suppression_is_untouched(cube):
    """No confidential column and no nulls: say nothing, add no noise."""
    out = _agg(cube, "plain", "v", dimensions=["country"])
    assert out["value"].get("suppressed", {}).get("v", 0) == 0
    assert "suppressed_by_flag" not in out["value"] or not out["value"]["suppressed_by_flag"]


# --- the count must match what the SUM actually excluded -------------------
# Found live 2026-09-21 on the real catches table, and it made the 1.2.12 guard
# inert exactly where it was needed. The cube types a column by what it holds,
# and total_live_weight_landed holds the placeholder 'C', so the column comes
# out VARCHAR. aggregate then sums it as TRY_CAST(col AS DOUBLE), which turns
# 'C' into NULL AT QUERY TIME and excludes it. The first version of this count
# asked whether the RAW column was NULL, where 'C' is a perfectly good string,
# so it reported 0 suppressed rows over a table that withholds 35.6% of them.
#
# The count has to ask the same question the SUM asked: how many rows did the
# cast drop. Otherwise the number is a lower bound that claims to be complete,
# which is worse than no count at all.

def _cube_with_placeholders(monkeypatch, tmp_path):
    import duckdb
    from surimi_data_mcp.provenance import lineage
    from surimi_data_mcp.engine.cube import Cube
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", str(tmp_path / "absent.json"))
    monkeypatch.delenv("SURIMI_CUBE_DB", raising=False)
    lineage.reset()
    con = duckdb.connect(":memory:")
    # VARCHAR measure, exactly as the cube infers it when 'C' is present.
    con.execute("CREATE TABLE catches_raw ("
                "country VARCHAR, total_live_weight_landed VARCHAR, confidential VARCHAR)")
    con.execute("INSERT INTO catches_raw VALUES "
                "('ESP','10.0','N'), ('ESP','20.0','N'), "
                "('ESP','C','A'), ('ESP','C','A'), ('NLD','5.0','N')")
    return Cube(con)


def test_a_non_numeric_placeholder_counts_as_suppressed(monkeypatch, tmp_path):
    from surimi_data_mcp.engine import aggregate as agg_mod
    cube = _cube_with_placeholders(monkeypatch, tmp_path)
    out = agg_mod.aggregate(cube, "catches_raw",
                            [{"col": "total_live_weight_landed", "fn": "sum"}],
                            dimensions=["country"])
    assert out["value"]["suppressed"]["total_live_weight_landed"] == 2


def test_the_flag_breakdown_works_on_a_varchar_measure(monkeypatch, tmp_path):
    from surimi_data_mcp.engine import aggregate as agg_mod
    cube = _cube_with_placeholders(monkeypatch, tmp_path)
    out = agg_mod.aggregate(cube, "catches_raw",
                            [{"col": "total_live_weight_landed", "fn": "sum"}],
                            dimensions=["country"])
    assert out["value"]["suppressed_by_flag"] == {"A": 2}


def test_the_note_fires_for_a_varchar_measure(monkeypatch, tmp_path):
    from surimi_data_mcp.engine import aggregate as agg_mod
    cube = _cube_with_placeholders(monkeypatch, tmp_path)
    out = agg_mod.aggregate(cube, "catches_raw",
                            [{"col": "total_live_weight_landed", "fn": "sum"}],
                            dimensions=["country"])
    assert "lower bound" in out["value"]["notes"].lower()


def test_the_sum_still_excludes_them(monkeypatch, tmp_path):
    """The arithmetic is unchanged; only the honesty about it changes."""
    from surimi_data_mcp.engine import aggregate as agg_mod
    cube = _cube_with_placeholders(monkeypatch, tmp_path)
    out = agg_mod.aggregate(cube, "catches_raw",
                            [{"col": "total_live_weight_landed", "fn": "sum"}],
                            dimensions=["country"])
    got = {r["country"]: r["total_live_weight_landed_sum"] for r in out["value"]["rows"]}
    assert got == {"ESP": 30.0, "NLD": 5.0}
