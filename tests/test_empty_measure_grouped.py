"""An all-zero measure must say so even when the call groups by a dimension.

The unreported-zero note existed and was reachable only when there were no
dimensions. Q-E6-4's call had them, so it was skipped in the case that matters
most: the model asked aggregate for y_2002 and y_2021 averages per species over
GSA 20, 22, 23, and got 438 groups whose y_2002_avg is 0.0 in every row.
y_2002 is zero on all 89,156 Aegean rows because the Aegean was not surveyed
that year, and nothing in the result distinguished that from a flat index. The
grounded answer uses 1994 to 2021, a range list_columns already reports.
"""
import duckdb
import pytest

from surimi_data_mcp.engine import aggregate as agg
from surimi_data_mcp.engine.cube import Cube


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "0" * 32)


@pytest.fixture
def wide_cube():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE survey AS SELECT * FROM (VALUES
            ('AAA', 20, 0.0, 1.5, 3.0),
            ('BBB', 20, 0.0, 2.5, 1.0),
            ('CCC', 22, 0.0, 0.5, 4.0),
            ('DDD', 29, 7.0, 9.9, 2.0)
        ) AS t(alpha3_code, gsa_area, y_2002, y_1994, y_2021)
    """)
    return Cube(con)


def _call(cube, **kw):
    base = dict(dataset="survey", measures=[{"col": "y_2002", "fn": "avg"}],
                dimensions=["alpha3_code"],
                filters=[{"col": "gsa_area", "op": "in", "value": [20, 22]}])
    base.update(kw)
    return agg.aggregate(cube, **base)


def test_a_grouped_all_zero_measure_is_called_unreported(wide_cube):
    out = _call(wide_cube)
    assert out["value"]["rows"], "the call must still return its groups"
    assert all(r["y_2002_avg"] in (0, 0.0) for r in out["value"]["rows"])
    notes = out["value"]["notes"].lower()
    assert "unreported" in notes, out["value"]["notes"]


def test_it_names_the_year_columns_that_do_carry_data(wide_cube):
    """The fact the model cannot guess. y_1994 and y_2021 have values under the
    same filters; y_2002 does not."""
    out = _call(wide_cube)
    assert "y_1994" in out["value"]["notes"] and "y_2021" in out["value"]["notes"], out["value"]["notes"]


def test_a_measure_with_real_values_gets_no_such_note(wide_cube):
    out = _call(wide_cube, filters=[{"col": "gsa_area", "op": "in", "value": [29]}])
    assert "unreported" not in out["value"]["notes"].lower(), out["value"]["notes"]


def test_a_partially_zero_measure_gets_no_such_note(wide_cube):
    out = _call(wide_cube, filters=[])
    assert "unreported" not in out["value"]["notes"].lower(), out["value"]["notes"]


def test_the_ungrouped_note_still_works(wide_cube):
    out = _call(wide_cube, dimensions=[])
    assert "unreported" in out["value"]["notes"].lower(), out["value"]["notes"]
