"""`engine/cube.py` must undo the same oecd.fsedata column rotation as `loader/load_csv.py`.

`aggregate` (the tool the model is told to always prefer for any number) goes
through the DuckDB cube, not Postgres, and `engine/cube.py` has its own, entirely
separate CSV ingestion path (DuckDB's read_csv_auto, no shared code with
`loader/load_csv.py`). The historic bug report --

    aggregate oecd-fsedata, sum(y_2013)  ->  {"y_2013_sum": 1646.0}, signed

-- was measured against the CUBE (the `aggregate` tool), which proves the
cube-side ingestion carries this same rotation and needs its own fix; fixing
only `loader/load_csv.py` would leave `aggregate` silently wrong.

See tests/test_load_csv_row_realignment.py for the measured shape of the
corruption itself (data rows carry [23 year fields][10 dimension fields]
while the header lists [10 dimension][23 year]).
"""
from __future__ import annotations

import csv as _csv
from pathlib import Path

import pytest

from surimi_data_mcp.engine.cube import _ROTATED_CUBE_DATASETS, build_cube


_HEADER = [
    "country.code", "country.name", "country.policy.code", "country.policy.name",
    "fse.category.code", "fse.category.name", "country.policy.description",
    "support.mechanism", "unit.of.measure.code", "unit.of.measure.name",
] + [str(y) for y in range(2000, 2023)]

# Physically [23 year fields][10 dimension fields] -- the measured corruption.
_CORRUPT_ROW = (
    [""] * 11 + ["7962777.30823013", "4658344.6914562", "4578726.24690849",
                 "6357012.343671", "5123370.6578622"] + ["0"] * 7
    + ["ARG", "Argentina", "IFINC_INS_ARG1",
       "Productive recovery programme (Argentina)", "S07",
       "Support to fisher income",
       "This program provides workers a fixed amount, up 12 months.",
       "Transfer", "USD", "US dollar"]
)


@pytest.fixture
def corrupt_fsedata(tmp_path: Path) -> Path:
    root = tmp_path / "lake"
    root.mkdir()
    path = root / "hive_oecd_fsedata.csv"
    with open(path, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(_HEADER)
        w.writerow(_CORRUPT_ROW)
    return root


def test_oecd_fsedata_is_the_only_rotated_cube_dataset():
    assert _ROTATED_CUBE_DATASETS == {"hive_oecd_fsedata": 23}


def test_cube_realigns_the_known_bad_dataset(corrupt_fsedata: Path):
    c = build_cube(str(corrupt_fsedata))
    rows = c.query('SELECT * FROM hive_oecd_fsedata')
    assert len(rows) == 1
    row = rows[0]

    assert row["country_code"] == "ARG"
    assert row["country_name"] == "Argentina"
    assert row["support_mechanism"] == "Transfer"

    # y_2013 must hold the real numeric value, not the string "ARG" the
    # un-fixed rotation would place there (header index 23 = "2013").
    assert row["y_2013"] == pytest.approx(4578726.24690849)
    assert row["y_2011"] == pytest.approx(7962777.30823013)
    assert row["y_2000"] is None


def test_aggregate_sum_over_a_year_column_no_longer_signs_a_country_code(corrupt_fsedata: Path):
    """The exact historic regression: sum(y_2013) must be the real number,
    not a count of ISO codes coerced through TRY_CAST."""
    c = build_cube(str(corrupt_fsedata))
    got = c.query("SELECT SUM(y_2013) AS s FROM hive_oecd_fsedata")[0]["s"]
    assert got == pytest.approx(4578726.24690849)


def test_unrotated_datasets_are_unaffected(corrupt_fsedata: Path):
    """A normal, non-rotated CSV alongside the bad one must load untouched."""
    clean_path = corrupt_fsedata / "clean.csv"
    with open(clean_path, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["year", "country_code", "landings"])
        w.writerow(["2020", "BEL", "100.5"])
    c = build_cube(str(corrupt_fsedata))
    row = c.query("SELECT * FROM clean")[0]
    assert row["country_code"] == "BEL"
    assert row["landings"] == pytest.approx(100.5)
