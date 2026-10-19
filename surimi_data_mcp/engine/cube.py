"""The DuckDB cube, a database that holds one table per SURIMI data-lake CSV.

The server calculates every total, average, ranking and count in this cube, so the LLM never adds up
rows itself. `build_cube` reads each CSV under a data directory into its own table and then registers
the semantic views from `surimi_data_mcp.datasets.semantic_views`. DuckDB runs inside the process,
so no database server is needed.

How the tables are built:
  * DuckDB's CSV sniffer infers each column type, so `SUM(landings_tonnes)` needs no cast and
    `year` is an integer.
  * Headers go through `_sanitize_headers`, the function the PostgreSQL loader uses, so a header such
    as `total.trips` or `Mean age of vessels` becomes a snake_case name. Two headers that sanitize to
    the same name get the suffixes `_2`, `_3` and so on, and no column is dropped.
  * A missing or empty data directory gives an empty cube and no error.
  * Each table is dropped and recreated, so building over an existing `.duckdb` file never
    duplicates rows.
  * `Cube.registry` reads the dataset list, column types and row counts from the cube itself.

Public names:
  build_cube(data_dir, db_path=":memory:") -> Cube
  get_cube() -> Cube
  Cube.datasets() -> list[str]
  Cube.columns(dataset) -> list[{name, type}]
  Cube.registry() -> list of registry entries, in the shape `BUILD_SCHEMA` describes
  Cube.query(sql) -> list[dict]
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

import duckdb

from surimi_data_mcp.loader.load_csv import _sanitize_headers
from surimi_data_mcp.datasets.semantic_views import apply_semantic_views
from surimi_data_mcp.paths import APP_ROOT

# File extensions read from the data directory.
_CSV_EXTS = (".csv",)

# The field names of a registry entry, as returned by `Cube.registry`.
BUILD_SCHEMA: dict[str, Any] = {
    "description": (
        "Cube registry: one entry per logical dataset, read back from DuckDB's "
        "information_schema so it cannot drift from the actual tables."
    ),
    "registry_entry": {
        "dataset": "logical dataset name (sanitized CSV file stem)",
        "source": "relative path of the source CSV under the data dir",
        "row_count": "exact COUNT(*) of rows loaded into the table",
        "columns": "list of column_entry (see below), in ordinal order",
    },
    "column_entry": {
        "name": "sanitized snake_case column identifier",
        "type": "DuckDB SQL data type (e.g. BIGINT, DOUBLE, VARCHAR)",
    },
}


def _sanitize_name(stem: str) -> str:
    """Turn a CSV file stem into a dataset name with the rules used for column headers.

    The result carries no numeric suffix, because the list passed to `_sanitize_headers` has one name.
    """
    return _sanitize_headers([stem])[0]


def _discover_csvs(data_dir: str) -> list[Path]:
    """List every CSV under `data_dir`, searched recursively and sorted by path.

    A missing directory gives an empty list. The sort fixes the order in which colliding dataset
    names get their numeric suffix.
    """
    base = Path(data_dir)
    if not base.is_dir():
        return []
    return sorted(
        p for p in base.rglob("*")
        if p.is_file() and p.suffix.lower() in _CSV_EXTS
    )


class Cube:
    """A DuckDB connection over the cube tables, with methods that list datasets and columns and run SQL."""

    def __init__(self, con: duckdb.DuckDBPyConnection):
        self._con = con

    # Introspection methods read from the live DuckDB catalog.
    def datasets(self) -> list[str]:
        """List the table and view names in the `main` schema of the cube, sorted.

        The semantic views are listed with the dataset tables. Names that start with `_cube_`, such as
        `_cube_sources`, are left out.
        """
        rows = self._con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name NOT LIKE '\\_cube\\_%' ESCAPE '\\' "
            "ORDER BY table_name"
        ).fetchall()
        return [r[0] for r in rows]

    def columns(self, dataset: str) -> list[dict[str, str]]:
        """List the `name` and DuckDB `type` of each column of `dataset`, in table order.

        `dataset` is passed to the query as a bound parameter.
        """
        rows = self._con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = ? "
            "ORDER BY ordinal_position",
            [dataset],
        ).fetchall()
        return [{"name": name, "type": dtype} for (name, dtype) in rows]

    def registry(self) -> list[dict[str, Any]]:
        """Return one entry per dataset with its `source` path, `row_count` and `columns`.

        The server recomputes the entries from `information_schema` and `COUNT(*)` on every call.
        `source` is an empty string when `_cube_sources` has no row for the dataset.
        """
        out: list[dict[str, Any]] = []
        for name in self.datasets():
            cols = self.columns(name)
            row_count = self._con.execute(
                f'SELECT COUNT(*) FROM "{name}"'
            ).fetchone()[0]
            source = self._con.execute(
                "SELECT source FROM _cube_sources WHERE dataset = ?", [name]
            ).fetchone()
            out.append({
                "dataset": name,
                "source": source[0] if source else "",
                "row_count": int(row_count),
                "columns": cols,
            })
        return out

    def query(self, sql: str) -> list[dict[str, Any]]:
        """Run `sql` and return each row as a dictionary from column name to value."""
        cur = self._con.execute(sql)
        col_names = [d[0] for d in cur.description]
        return [dict(zip(col_names, row)) for row in cur.fetchall()]

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        """The DuckDB connection, which `aggregate` uses to run its own SQL."""
        return self._con

    def close(self) -> None:
        """Close the DuckDB connection."""
        self._con.close()


def _create_sources_table(con: duckdb.DuckDBPyConnection) -> None:
    """Create the `_cube_sources` table that maps each dataset to its source path, replacing any old one."""
    con.execute("DROP TABLE IF EXISTS _cube_sources")
    con.execute(
        "CREATE TABLE _cube_sources (dataset VARCHAR PRIMARY KEY, source VARCHAR)"
    )


# Encodings that DuckDB tries in order, UTF-8 first and then latin-1 for ISO-8859-1 files with accented
# names. The load never sets `ignore_errors`, which would drop rows and change every aggregate.
_ENCODINGS = ("utf-8", "latin-1")


def _read_csv_sql(path_literal: str, encoding: str) -> str:
    """Return the DuckDB `read_csv_auto(...)` call for `path_literal` with the given encoding.

    `all_varchar=false` keeps per-column type inference, `strict_mode=false` accepts rows with extra or
    missing fields, and `sample_size=-1` reads the whole file to infer types, so a column that is
    numeric for many rows and text later is typed VARCHAR.
    """
    return (
        f"read_csv_auto('{path_literal}', all_varchar=false, "
        f"strict_mode=false, sample_size=-1, encoding='{encoding}')"
    )


# Datasets whose data rows are rotated against their own header (see
# tests/test_load_csv_row_realignment.py). `_ROTATED_TABLES` in loader/load_csv.py fixes the same file
# for PostgreSQL, but the cube reads CSVs with DuckDB and needs its own entry, keyed by dataset name.
# Value: the number of fields to rotate each row left by so that it matches its header.
_ROTATED_CUBE_DATASETS: dict[str, int] = {"hive_oecd_fsedata": 23}

# Datasets whose export mixes records with continuation fragments (see `_RECORD_KEY` in
# loader/load_csv.py). Value: the header position of the key and the pattern that a real record's key
# matches. Rows whose key does not match are left out of the table.
_CUBE_RECORD_KEY: dict[str, tuple[int, str]] = {"hive_oecd_fsedata": (0, "^[A-Z]{3}$")}


def _realign_columns(raw_headers: list[str], dataset: str) -> list[str]:
    """Return `raw_headers` reordered so that position `j` names the raw column holding the value for header `j`.

    This undoes the rotation listed for `dataset` in `_ROTATED_CUBE_DATASETS`. Any other dataset gets
    `raw_headers` back unchanged.
    """
    rot = _ROTATED_CUBE_DATASETS.get(dataset)
    if not rot:
        return raw_headers
    n = len(raw_headers)
    return [raw_headers[(rot + j) % n] for j in range(n)]


def _materialize(
    con: duckdb.DuckDBPyConnection,
    reader: str,
    dataset: str,
    rel_source: str,
) -> None:
    """Create the table `dataset` from `reader` with sanitized column names and record its source.

    Columns are renamed by position so DuckDB keeps the types it inferred. For a dataset listed in
    `_ROTATED_CUBE_DATASETS` each sanitized name takes its value from the raw column that holds it,
    and a dataset in `_CUBE_RECORD_KEY` keeps only the rows whose key matches. An existing table of the
    same name is dropped first.
    """
    rel = con.sql(f"SELECT * FROM {reader}")
    raw_headers = list(rel.columns)
    sanitized = _sanitize_headers(raw_headers)
    source_headers = _realign_columns(raw_headers, dataset)
    select_list = ", ".join(
        f'"{raw}" AS "{clean}"' for raw, clean in zip(source_headers, sanitized)
    )
    where = ""
    key = _CUBE_RECORD_KEY.get(dataset)
    if key:
        pos, pattern = key
        where = (f" WHERE regexp_matches(trim(CAST(\"{source_headers[pos]}\" AS VARCHAR)), "
                 f"'{pattern}')")
    con.execute(f'DROP TABLE IF EXISTS "{dataset}"')
    con.execute(f'CREATE TABLE "{dataset}" AS SELECT {select_list} FROM {reader}{where}')
    con.execute(
        "INSERT OR REPLACE INTO _cube_sources(dataset, source) VALUES (?, ?)",
        [dataset, rel_source],
    )


def _transcode_to_utf8(path: Path) -> str:
    """Decode a CSV as cp1252 into a temporary UTF-8 file and return that file's path.

    `_load_one_csv` calls this only when DuckDB rejects both encodings in `_ENCODINGS`. The caller
    deletes the temporary file, and a byte that cp1252 does not define raises `UnicodeDecodeError`.
    """
    raw = path.read_bytes()
    text = raw.decode("cp1252")
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
    )
    try:
        tmp.write(text)
    finally:
        tmp.close()
    return tmp.name


def _load_one_csv(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    dataset: str,
    rel_source: str,
) -> None:
    """Load one CSV into the table `dataset`, trying three ways of reading its encoding.

    DuckDB first reads the file as UTF-8 and then as latin-1. If both fail, the file is transcoded
    through cp1252 and loaded from the copy. When all three fail, the function raises `RuntimeError`.
    """
    path_str = str(path).replace("'", "''")  # literal-safe for the engine

    last_err: Optional[Exception] = None
    for encoding in _ENCODINGS:
        try:
            _materialize(con, _read_csv_sql(path_str, encoding), dataset, rel_source)
            return
        except duckdb.InvalidInputException as err:
            last_err = err
            continue

    # Last resort: transcode to UTF-8 in Python.
    tmp_path: Optional[str] = None
    try:
        tmp_path = _transcode_to_utf8(path)
        tmp_literal = tmp_path.replace("'", "''")
        _materialize(con, _read_csv_sql(tmp_literal, "utf-8"), dataset, rel_source)
        return
    except (duckdb.InvalidInputException, UnicodeDecodeError, OSError) as err:
        last_err = err
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Every strategy failed, so no empty table is created.
    raise RuntimeError(
        f"could not load CSV {rel_source!r} (utf-8/latin-1/cp1252): {last_err}"
    )


def build_cube(data_dir: str, db_path: str = ":memory:") -> Cube:
    """Build a DuckDB cube from every CSV under `data_dir` and apply the semantic views.

    Args:
        data_dir: root of the data lake, searched recursively for `*.csv`. A missing or empty
            directory gives an empty cube.
        db_path: `:memory:` (default) or the path of a persistent `.duckdb` file.

    Returns:
        A `Cube` over the new tables.
    """
    con = duckdb.connect(db_path)
    _create_sources_table(con)

    base = Path(data_dir)
    used_names: dict[str, int] = {}
    for path in _discover_csvs(data_dir):
        # The dataset name comes from the file stem. A second stem that sanitizes to the same name
        # gets a numeric suffix (for example "odd headers.csv" and "odd.headers.csv").
        name = _sanitize_name(path.stem)
        if name in used_names:
            used_names[name] += 1
            name = f"{name}_{used_names[name]}"
        else:
            used_names[name] = 1

        try:
            rel_source = str(path.relative_to(base))
        except ValueError:
            rel_source = str(path)
        _load_one_csv(con, path, name, rel_source)

    apply_semantic_views(con)

    return Cube(con)


# The process-wide cube that `get_cube` creates on first use.
_CUBE: Optional[Cube] = None


def _data_dir() -> str:
    """Return `SURIMI_DATA_DIR`, or the `data/exports` directory under `APP_ROOT` when it is unset."""
    env = os.environ.get("SURIMI_DATA_DIR")
    if env:
        return env
    return str(APP_ROOT / "data" / "exports")


def cube_is_stale(db_path: str, data_dir: str | None = None) -> bool:
    """Return True when the cube file at `db_path` lacks a table for a CSV in the data directory.

    The check compares the sanitized names of the CSV files directly in `data_dir` (default
    `SURIMI_DATA_DIR`) with the tables in the cube and logs a warning that names up to six missing
    datasets. It does not look in subdirectories and does not hash file contents, so an edited CSV
    with an unchanged name is not detected. A missing, in-memory or unreadable cube counts as stale,
    and a missing or CSV-free data directory does not.
    """
    if not db_path or db_path == ":memory:" or not os.path.exists(db_path):
        return True
    d = data_dir or _data_dir()
    if not d or not os.path.isdir(d):
        return False
    expected = {_sanitize_name(os.path.splitext(f)[0])
                for f in os.listdir(d) if f.lower().endswith(".csv")}
    if not expected:
        return False
    try:
        con = duckdb.connect(db_path, read_only=True)
        try:
            have = {r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='main'").fetchall()}
        finally:
            con.close()
    except Exception:
        return True
    missing = expected - have
    if missing:
        logging.getLogger(__name__).warning(
            "cube at %s is missing %d dataset(s) present as CSV: %s; rebuilding",
            db_path, len(missing), sorted(missing)[:6])
    return bool(missing)


def _is_prebuilt_cube(db_path: str) -> bool:
    """Return True when `db_path` is an existing DuckDB file with at least one dataset table.

    `get_cube` opens such a file, the prebuilt cube of a deployment, instead of building a new one.
    """
    if not db_path or db_path == ":memory:" or not os.path.exists(db_path):
        return False
    try:
        con = duckdb.connect(db_path, read_only=True)
        try:
            n = con.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema='main' AND table_name NOT LIKE '\\_cube\\_%' ESCAPE '\\'"
            ).fetchone()[0]
        finally:
            con.close()
        return n > 0
    except Exception:
        return False


def get_cube() -> Cube:
    """Return the process-wide cube, creating it on the first call.

    When `SURIMI_CUBE_DB` names a prebuilt cube that `cube_is_stale` accepts, the server opens it
    read-only. Otherwise it builds the cube from the CSVs in `SURIMI_DATA_DIR`, into that file when
    `SURIMI_CUBE_DB` is set and in memory when it is not.
    """
    global _CUBE
    if _CUBE is None:
        db = os.environ.get("SURIMI_CUBE_DB", ":memory:")
        if _is_prebuilt_cube(db) and not cube_is_stale(db):
            _CUBE = Cube(duckdb.connect(db, read_only=True))
        else:
            _CUBE = build_cube(_data_dir(), db_path=db)
    return _CUBE
