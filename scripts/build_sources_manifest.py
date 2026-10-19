#!/usr/bin/env python3
"""Write sources_manifest.json, the source-file record that the server attaches to its calculation records.

Usage: build_sources_manifest.py <csv_dir> <out_json>

The script scans <csv_dir> for the CSVs that `cube.build_cube` loads and keys each one by
the dataset name the cube gives it, using the same helper functions. Run it where the cube is
built and put the JSON next to cube.duckdb.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# The helpers of cube.py find the files, name the datasets and read the CSVs, so the
# manifest lists the same files, names and reader options as the cube.
from surimi_data_mcp.engine.cube import (
    _ENCODINGS,
    _discover_csvs,
    _read_csv_sql,
    _sanitize_name,
    _transcode_to_utf8,
)

# Upstream agency by filename prefix. The first match in dict order wins, so more
# specific prefixes come before shorter ones.
AGENCIES = {
    "hive_eu_dcf_aer": "STECF AER",
    "hive_eu_dcf_fdi": "STECF FDI",
    "hive_eu_dcf": "EU DCF (STECF)",
    "hive_eumofa": "EUMOFA",
    "hive_fao": "FAO",
    "hive_oecd": "OECD",
    "hive_surimi": "SURIMI project",
}


def _agency(stem: str) -> str:
    """Return the agency whose prefix starts ``stem``, or ``unknown``."""
    for prefix, name in AGENCIES.items():
        if stem.startswith(prefix):
            return name
    return "unknown"


def _sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of the file at ``path``."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _scan_stats(con: duckdb.DuckDBPyConnection, path_literal: str, encoding: str) -> dict:
    """Scan one CSV and return its row count and, when it has a `year` column, the years it covers.

    The `year` column is matched without regard to case. The result has `n_rows`, `first`
    and `last` (the lowest and highest year in the data) and `missing`, the years between them that no row carries.
    Year values that are not integers are ignored, and the three year fields are `None` or empty without a `year` column.
    """
    reader = _read_csv_sql(path_literal, encoding)
    columns = con.sql(f"SELECT * FROM {reader} LIMIT 0").columns
    year_col = next((c for c in columns if c.strip().lower() == "year"), None)
    if year_col is None:
        n_rows = con.execute(f"SELECT COUNT(*) FROM {reader}").fetchone()[0]
        return {"n_rows": int(n_rows), "first": None, "last": None, "missing": []}

    row = con.execute(
        f'SELECT COUNT(*), MIN(y), MAX(y), list(DISTINCT y) FROM '
        f'(SELECT TRY_CAST("{year_col}" AS BIGINT) AS y FROM {reader}) t'
    ).fetchone()
    n_rows, lo, hi, years = row
    if lo is None:
        return {"n_rows": int(n_rows), "first": None, "last": None, "missing": []}
    present = {int(y) for y in years if y is not None}
    missing = sorted(set(range(int(lo), int(hi) + 1)) - present)
    return {"n_rows": int(n_rows), "first": int(lo), "last": int(hi), "missing": missing}


def _stats(con: duckdb.DuckDBPyConnection, path: Path) -> dict:
    """Return the scan of one CSV, read with the reader options and encoding order of `cube._load_one_csv`.

    The encodings are tried as UTF-8, then latin-1, then a cp1252 transcode to UTF-8. It raises `RuntimeError` when none works.
    """
    path_literal = str(path).replace("'", "''")
    last_err: Exception | None = None
    for encoding in _ENCODINGS:
        try:
            return _scan_stats(con, path_literal, encoding)
        except duckdb.InvalidInputException as err:
            last_err = err

    tmp_path: str | None = None
    try:
        tmp_path = _transcode_to_utf8(path)
        return _scan_stats(con, tmp_path.replace("'", "''"), "utf-8")
    except (duckdb.InvalidInputException, UnicodeDecodeError, OSError) as err:
        last_err = err
    finally:
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass
    raise RuntimeError(f"could not scan {path} (utf-8/latin-1/cp1252): {last_err}")


def build(csv_dir: str) -> dict:
    """Scan every CSV under `csv_dir` and return the manifest as a dict keyed by dataset name.

    Each file gets its own DuckDB connection, so a failed scan does not affect the next file. A file that cannot be scanned stays in the manifest with an `error` message.
    """
    manifest = {}
    base = Path(csv_dir)
    # The same sorted order and "_2", "_3" suffixes as `cube.build_cube`, so the keys
    # equal the cube's dataset names when two sanitized stems collide.
    used_names: dict[str, int] = {}
    for path in _discover_csvs(csv_dir):
        stem = _sanitize_name(path.stem)
        if stem in used_names:
            used_names[stem] += 1
            stem = f"{stem}_{used_names[stem]}"
        else:
            used_names[stem] = 1
        con = duckdb.connect(":memory:")
        try:
            stats = _stats(con, path)
            err = None
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            stats = {"n_rows": None, "first": None, "last": None, "missing": []}
            err = f"{type(exc).__name__}: {exc}"
        finally:
            con.close()
        manifest[stem] = {
            # Path relative to the lake root with forward slashes, as `rel_source` in `cube.build_cube`.
            "file": path.relative_to(base).as_posix(),
            "sha256": _sha256(path),
            "n_rows": stats["n_rows"],
            "agency": _agency(stem),
            "error": err,
            "original_url": None,
            # The first and last year in the data and the years between them with no
            # row. They are null and [] when the file has no `year` column.
            "first": stats["first"],
            "last": stats["last"],
            "missing": stats["missing"],
        }
    return manifest


def main(csv_dir: str, out_json: str) -> None:
    """Write the manifest of ``csv_dir`` to ``out_json`` and print the number of entries and any unscannable files."""
    manifest = build(csv_dir)
    Path(out_json).write_text(
        json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    bad = [k for k, v in manifest.items() if v.get("error")]
    print(f"wrote {len(manifest)} entries -> {out_json}"
          + (f" ({len(bad)} unscannable: {bad})" if bad else ""))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
