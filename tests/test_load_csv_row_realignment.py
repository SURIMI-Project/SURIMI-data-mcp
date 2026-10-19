"""Repair for the oecd.fsedata column rotation (Q-E2-3).

hive_oecd_fsedata.csv ships a header in the order [10 dimension columns, 23
year columns] (country.code .. unit.of.measure.name, then 2000..2022), but
every DATA ROW physically carries its fields in the OPPOSITE block order: the
23 year values first, the 10 dimension values last. Measured directly against
the real export (data/exports/hive_oecd_fsedata.csv) and cross-checked against
the true upstream source (FSEdata.csv, which is NOT rotated): row 1's
country.code cell literally holds a year-2001 value, and the true country code
("ARG") sits under the header labelled "2013".

The two field blocks are simply swapped: the correct value for header
position j is the row's raw field at position (23 + j) % 33 -- i.e. rotating
the 33-field row left by 23 (equivalently right by 10) recovers the row the
header describes. This is data-level, not a CSV-quoting bug: csv.reader
already parses every row to exactly 33 fields (the free-text description
field IS properly quoted), so the historic "unescaped commas" theory was
wrong about the mechanism even though the symptom (rotation) was real.

The Postgres loader (`loader/load_csv.py`) applies `_realign_row` and the DuckDB loader
(`engine/cube.py`) applies `_realign_columns`, both to the same known-bad table. This file
pins the Postgres side.
"""
from __future__ import annotations

from pathlib import Path

from surimi_data_mcp.loader.load_csv import (
    CSV_TO_TABLE,
    _ROTATED_TABLES,
    _realign_row,
    _infer_table_types,
)

# The exact shape measured on the real export (values shortened but the
# block-swap is faithful): 10 dimension fields, then 23 year fields, per the
# true header order -- but written into each ROW as [years..., dims...].
_HEADER = (
    "country.code,country.name,country.policy.code,country.policy.name,"
    "fse.category.code,fse.category.name,country.policy.description,"
    "support.mechanism,unit.of.measure.code,unit.of.measure.name,"
    "2000,2001,2002,2003,2004,2005,2006,2007,2008,2009,2010,2011,2012,"
    "2013,2014,2015,2016,2017,2018,2019,2020,2021,2022\n"
)

# Row physically ordered [23 year fields][10 dimension fields] -- the real
# corruption. Only y2011..y2015 carry values; the rest are the blank/zero
# pattern measured on the live file.
_CORRUPT_ROW = (
    ",,,,,,,,,,,7962777.30823013,4658344.6914562,4578726.24690849,"
    "6357012.343671,5123370.6578622,0,0,0,0,0,0,0,"
    "ARG,Argentina,IFINC_INS_ARG1,"
    '"Productive recovery programme (Argentina)",'
    "S07,Support to fisher income,"
    '"This program provides workers a fixed amount, up 12 months.",'
    "Transfer,USD,US dollar\n"
)


def test_oecd_fsedata_is_the_only_rotated_table():
    """Extending this requires first proving the new table is rotated the
    same way -- do not just add a name here."""
    assert _ROTATED_TABLES == {"oecd.fsedata": 23}


def test_unrotated_table_is_untouched():
    row = ["a", "b", "c"]
    assert _realign_row(row, "eu_dcf.fleet_economics") == row
    assert _realign_row(row, "eu_dcf.fleet_economics") is row


def test_realign_row_recovers_the_true_field_order():
    import csv as _csv
    import io

    row = next(_csv.reader(io.StringIO(_CORRUPT_ROW)))
    assert len(row) == 33

    fixed = _realign_row(row, "oecd.fsedata")

    # Dimension block (was at the END of the row) is now FIRST, matching the
    # header's declared order.
    assert fixed[0] == "ARG"                 # country.code
    assert fixed[1] == "Argentina"           # country.name
    assert fixed[2] == "IFINC_INS_ARG1"      # country.policy.code
    assert fixed[7] == "Transfer"            # support.mechanism
    assert fixed[8] == "USD"                 # unit.of.measure.code
    assert fixed[9] == "US dollar"           # unit.of.measure.name

    # Year block (was at the START of the row) is now LAST, in the same
    # internal order it was measured in on the live file: 2000-2010 blank,
    # 2011-2015 the five real values, 2016-2022 zero.
    assert fixed[10] == ""                   # 2000
    assert fixed[20] == ""                   # 2010
    assert fixed[21] == "7962777.30823013"   # 2011
    assert fixed[25] == "5123370.6578622"    # 2015
    assert fixed[26] == "0"                  # 2016
    assert fixed[32] == "0"                  # 2022
    assert len(fixed) == 33


def test_realign_row_is_its_own_inverse_at_the_measured_width():
    """A 33-field row rotated by 23 and then again by 23 (mod 33 = 10) is NOT
    a no-op -- guards against silently applying the fix twice (e.g. once in
    loader/load_csv.py and once more in a future refactor) and getting a third,
    different-and-wrong alignment instead of a loud failure."""
    import csv as _csv
    import io

    row = next(_csv.reader(io.StringIO(_CORRUPT_ROW)))
    once = _realign_row(row, "oecd.fsedata")
    twice = _realign_row(once, "oecd.fsedata")
    assert once != row
    assert twice != once


def test_infer_table_types_sees_the_realigned_values(tmp_path: Path):
    """Type inference must run over the REALIGNED rows, not the raw ones, or
    y_2013 (a real numeric year column) infers TEXT because it saw a mix of
    ISO country codes and prose across the corrupted file, and country_code
    infers wrongly too."""
    path = tmp_path / "hive_oecd_fsedata.csv"
    path.write_text(_HEADER + _CORRUPT_ROW)

    headers, types = _infer_table_types(str(path), "oecd.fsedata")
    assert headers[0] == "country.code"

    # Index 21 is header "2011" -- with realignment applied its one value
    # (7962777.3...) is a genuine fractional number, so it must NOT be
    # dragged to TEXT by the corrupted alignment (which would have put the
    # literal string "ARG" three slots later, at index 23 = "2013", instead).
    assert types[21] in ("DOUBLE PRECISION",)
    assert types[0] == "TEXT"  # country.code: text, whatever it infers as


def test_csv_to_table_still_maps_the_fsedata_export():
    """Re-registering the dataset must not silently drop the load target."""
    assert CSV_TO_TABLE["hive_oecd_fsedata.csv"] == "oecd.fsedata"
