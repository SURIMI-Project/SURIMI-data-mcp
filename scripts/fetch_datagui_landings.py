"""Normalise the three DataGUI landings CSVs into two tables for the data server.

The script reads the project's own copy of the files under ``mcp_data/data/1_Fisheries``
in the bucket ``project-surimi`` and does not write to the bucket.

The three files cover different FAO areas and differ in three ways. The division-level
file spells `fao_subdivion` without the second "s", only the area-level ICES file has
`fao_unit`, and the taxonomic codes were stored as floats upstream. The code handles each
difference explicitly.

The ICES area-level file and the ICES division-level file describe the same area-27 catch
at two granularities, so they would be counted twice in one table. `write_union` therefore
writes two CSV files, one per granularity. The Mediterranean file covers a different area
and goes into the same table as the area-level ICES file.
"""
from __future__ import annotations

import csv
import io
import os
import re
from typing import Optional

BUCKET = "project-surimi"
# The project's own copy of the DataGUI landings files. The keys under it contain
# spaces and capitals, which differ from the snake_case naming of shared buckets,
# because they match the layout already in the bucket. BUCKET, PREFIX and SOURCES are the
# only values to change when that prefix is renamed.
PREFIX = "mcp_data/data/1_Fisheries"

SOURCES = {
    "mediterranean_and_black_sea.csv":
        f"{PREFIX}/1.1_FAO LANDINGS FAO 37/Mediterranean and Black Sea.csv",
    "atlantic_northeast_ices.csv":
        f"{PREFIX}/1.2_ICES LANDINGS FAO 27/1. LANDINGS AREA 27_ICES/Atlantic_Northeast_ices.csv",
    "atlantic_northeast_division_ices.csv":
        f"{PREFIX}/1.2_ICES LANDINGS FAO 27/2. DIVISION LEVEL/Atlantic_Northeast_Division_ices.csv",
}

# The output CSV that each source file goes into. The area-level ICES file and the
# FAO 37 Mediterranean file cover different areas and share one table. The
# division-level ICES file covers the same area-27 catch as the area-level file and gets its own table.
_LANDINGS_SOURCES = ("mediterranean_and_black_sea.csv", "atlantic_northeast_ices.csv")
_DIVISIONS_SOURCES = ("atlantic_northeast_division_ices.csv",)

_OUTPUT_FILES = {
    "hive_fisheries_fisheries_landings.csv": _LANDINGS_SOURCES,
    "hive_fisheries_fisheries_landings_divisions.csv": _DIVISIONS_SOURCES,
}

# Column renames applied before the rows are combined, from the misspelled name to the correct one.
_RENAME = {"fao_subdivion": "fao_subdivision"}

# The columns of both output files, sorted so the header is the same on every run.
UNION_COLUMNS = sorted({
    "alpha3_code", "common_name", "country", "country_harmonized",
    "fao_area", "fao_area_description", "fao_division",
    "fao_division_description", "fao_subarea", "fao_subarea_description",
    "fao_subdivision", "fao_unit", "landings", "original_area",
    "scient_name", "source", "source_file", "taxonomic_code", "year",
})

# Missing-value tokens, compared without regard to case. `normalise_row` applies them to
# every column, so a dimension column that holds "NA", "none", "null" or "nan" becomes NULL too.
_MISSING = {"", "na", "n/a", "nan", "null", "none"}

# A number with thousands separators: groups of exactly three digits after the first,
# an optional leading minus and an optional decimal part. "1,234" and "-1,234,567.5"
# match and "1,23" does not. Commas are removed only from a value of this shape.
_THOUSANDS_RE = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")


def _num(v) -> Optional[float]:
    """Parse ``v`` as a float, removing thousands separators, and return ``None`` for a missing or unparseable value."""
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() in _MISSING:
        return None
    if _THOUSANDS_RE.match(s):
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _normalise_keys(row: dict) -> dict:
    """Lowercase and strip every key of ``row`` and rename the misspelled ones.

    When a row has both a misspelled column and its correct spelling, the correct one is kept.
    """
    raw = {k.strip().lower(): v for k, v in (row or {}).items()}
    src = {k: v for k, v in raw.items() if k not in _RENAME}
    for misspelled, canonical in _RENAME.items():
        if misspelled in raw and canonical not in src:
            src[canonical] = raw[misspelled]
    return src


def normalise_row(row: dict, source_file: str) -> dict:
    """Turn one raw CSV row into a row with the columns of `UNION_COLUMNS`, and add the name of its source file.

    `landings` is parsed as a number and `year` as an integer. A blank or missing value
    becomes `None` in every column, using the tokens in `_MISSING`. `taxonomic_code` stays a string.
    """
    src = _normalise_keys(row)
    out: dict = {c: None for c in UNION_COLUMNS}
    for c in UNION_COLUMNS:
        if c in src:
            v = src[c]
            s = None if v is None or str(v).strip().lower() in _MISSING else str(v).strip()
            out[c] = s
    out["landings"] = _num(src.get("landings"))
    out["year"] = int(_num(src.get("year"))) if _num(src.get("year")) is not None else None
    out["source_file"] = source_file
    return out


def _client():
    """Return an S3 client for the host in ``AWS_S3_ENDPOINT``, or the default AWS endpoint when it is unset."""
    import boto3
    ep = os.environ.get("AWS_S3_ENDPOINT", "")
    return boto3.client("s3", endpoint_url=f"https://{ep}" if ep else None)


def _landings_is_unparseable(row: dict) -> bool:
    """Return True when the raw `landings` value is present and not a missing token, yet does not parse as a number."""
    raw = _normalise_keys(row).get("landings")
    if raw is None:
        return False
    s = str(raw).strip()
    if s.lower() in _MISSING:
        return False
    return _num(raw) is None


def write_union(dest_dir: str, client=None) -> dict[str, int]:
    """Write the three sources into two CSV files under `dest_dir` and return the row count of each, keyed by file name.

    `hive_fisheries_fisheries_landings.csv` holds the Mediterranean and the area-level ICES rows.
    `hive_fisheries_fisheries_landings_divisions.csv` holds the division-level ICES rows.

    Each source is read from S3 in full and its rows are written one at a time. A `landings` value that
    is present but does not parse is written as empty and counted, and the function prints one
    ``WARNING: <filename>: N landings values unparseable`` line for each file that has any.
    """
    s3 = client or _client()
    os.makedirs(dest_dir, exist_ok=True)
    counts = {name: 0 for name in _OUTPUT_FILES}
    unparseable = {name: 0 for name in _OUTPUT_FILES}

    writers: dict[str, csv.DictWriter] = {}
    handles = []
    try:
        for name in _OUTPUT_FILES:
            fh = open(os.path.join(dest_dir, name), "w", newline="", encoding="utf-8")
            handles.append(fh)
            w = csv.DictWriter(fh, fieldnames=UNION_COLUMNS)
            w.writeheader()
            writers[name] = w

        for label, key in SOURCES.items():
            body = s3.get_object(Bucket=BUCKET, Key=key)["Body"]
            text = io.TextIOWrapper(io.BytesIO(body.read()), encoding="utf-8", errors="replace")
            out_name = next(name for name, sources in _OUTPUT_FILES.items() if label in sources)
            w = writers[out_name]
            for row in csv.DictReader(text):
                if _landings_is_unparseable(row):
                    unparseable[out_name] += 1
                w.writerow(normalise_row(row, label))
                counts[out_name] += 1
    finally:
        for fh in handles:
            fh.close()

    for name in _OUTPUT_FILES:
        if unparseable[name] > 0:
            print(f"WARNING: {name}: {unparseable[name]} landings values unparseable")

    return counts
