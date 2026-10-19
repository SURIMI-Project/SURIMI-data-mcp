"""Prepare the data, the cube and the docs that the data server reads at start-up.

The data source is either a local directory or an ``s3://bucket/prefix`` URI, so the
Helm chart can fill the server at init time without baking the lake into the image.
`bootstrap` resolves the data and docs sources (downloading from MinIO or S3 for an
``s3://`` URI) and then runs three steps.

1. It builds the DuckDB cube from the data directory with `cube.build_cube`.
2. It loads the CSVs named in `ANALYTIC_CSVS` into PostgreSQL for row-level `query_data`
   calls, through `surimi_data_mcp.loader.load_csv`. The other CSVs stay in the cube only.
   With `postgres_url=None` this step is skipped.
3. It returns the local docs directory, which the server reads through `SURIMI_DOCS_DIRS`.
   The docs index is built later by `docsearch`, not here.

Re-running over the same source rebuilds the same cube and reloads the same tables
without duplicate rows. An empty or missing local source gives an empty cube and a
summary with zero datasets. Progress lines are flushed at once so the init logs stream.

Public API:
  bootstrap(data_src, docs_src, cube_db, postgres_url=None,
            analytic_datasets=None, build_cube=True) -> summary dict
  ANALYTIC_CSVS  -> the file names loaded into PostgreSQL by default
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from surimi_data_mcp.engine import cube as cube_mod
from surimi_data_mcp.loader.load_csv import CSV_TO_TABLE

# The CSVs loaded into PostgreSQL for `query_data`, taken from the `CSV_TO_TABLE` map of
# `load_csv.py` so the two lists cannot differ. Raw spatial and survey files are not in
# that map and stay in the cube only.
#
# EU_DCF_Catches.csv (1.9 GB) is not in the map on purpose. Its measures hold a
# confidentiality flag ("C") instead of a value, and loading the flag as a number would
# turn a suppressed record into a zero and understate every national sum.
# `tests/test_catalog_additions.py` checks that it stays out.
ANALYTIC_CSVS: tuple[str, ...] = tuple(CSV_TO_TABLE.keys())

# S3 endpoint environment variables, in the order they are tried (MinIO needs a custom endpoint).
_ENDPOINT_ENV = ("AWS_ENDPOINT_URL", "S3_ENDPOINT_URL")


def _log(msg: str) -> None:
    """Print one flushed progress line so the chart-init logs stream as they happen."""
    print(f"[bootstrap] {msg}", flush=True)


# ───────────────────────────── source resolution ────────────────────────────
def _is_s3(uri: str) -> bool:
    return uri.startswith("s3://")


def _parse_s3(uri: str) -> tuple[str, str]:
    """Split ``s3://bucket/prefix/...`` into (bucket, prefix), where the prefix may be empty."""
    rest = uri[len("s3://"):]
    bucket, _, prefix = rest.partition("/")
    if not bucket:
        raise ValueError(f"invalid s3 URI (no bucket): {uri!r}")
    return bucket, prefix.strip("/")


def _s3_client():
    """Return a boto3 S3 client configured from the environment.

    Credentials come from the standard `AWS_*` variables, including `AWS_SESSION_TOKEN`.
    The endpoint comes from `AWS_ENDPOINT_URL` or `S3_ENDPOINT_URL` when one is set, and is AWS S3 otherwise.
    """
    import boto3

    endpoint = None
    for key in _ENDPOINT_ENV:
        val = os.environ.get(key)
        if val:
            endpoint = val
            break
    kwargs: dict[str, Any] = {}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    # Some clients need a region, and MinIO ignores the value.
    kwargs.setdefault("region_name", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    return boto3.client("s3", **kwargs)


def _download_s3_prefix(uri: str, dest: Path) -> int:
    """Download every object under an ``s3://bucket/prefix`` into the local directory ``dest``.

    The key path below the prefix becomes the local relative path, so the cube sees the
    layout of a local lake. It returns the number of objects fetched and raises
    `FileNotFoundError` when the prefix holds no objects.
    """
    bucket, prefix = _parse_s3(uri)
    client = _s3_client()
    dest.mkdir(parents=True, exist_ok=True)

    paginator = client.get_paginator("list_objects_v2")
    list_kwargs: dict[str, Any] = {"Bucket": bucket}
    if prefix:
        list_kwargs["Prefix"] = prefix + "/"

    fetched = 0
    for page in paginator.paginate(**list_kwargs):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            if key.endswith("/"):
                continue  # directory placeholder
            # The path relative to the lake root is the key without the prefix.
            rel = key[len(prefix):].lstrip("/") if prefix else key
            if not rel:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(target))
            fetched += 1
    if fetched == 0:
        raise FileNotFoundError(
            f"no objects found under {uri!r} (empty or wrong bucket/prefix)"
        )
    _log(f"downloaded {fetched} object(s) from {uri} -> {dest}")
    return fetched


def _resolve_source(src: str, tmp_root: Path, label: str) -> str:
    """Return the local directory for a data or docs source.

    A local path is returned as it is, without a copy, even when it does not exist. An
    ``s3://`` URI is downloaded into the directory ``label`` under `tmp_root`, and that
    directory is returned.
    """
    if _is_s3(src):
        dest = tmp_root / label
        _download_s3_prefix(src, dest)
        return str(dest)
    _log(f"{label} source is a local dir: {src}")
    return src


# ───────────────────────────── Postgres subset ──────────────────────────────
def _select_analytic_csvs(
    data_dir: str,
    analytic_datasets: Optional[Sequence[str]],
) -> list[Path]:
    """Find the CSV files under ``data_dir`` that are loaded into PostgreSQL.

    Files are matched by name against ``analytic_datasets``, which defaults to
    `ANALYTIC_CSVS`. Only files that exist on disk are returned, in the order of that list.
    """
    wanted = list(analytic_datasets) if analytic_datasets is not None else list(ANALYTIC_CSVS)
    wanted_set = set(wanted)
    base = Path(data_dir)
    if not base.is_dir():
        return []
    # Index file names once. The search is recursive because the lake nests its CSVs in folders.
    found: dict[str, Path] = {}
    for p in base.rglob("*.csv"):
        if p.is_file():
            found.setdefault(p.name, p)
    # Keep the order of the list and drop names that are not on disk.
    return [found[name] for name in wanted if name in wanted_set and name in found]


def _default_pg_loader(csv_paths: Iterable[Path], postgres_url: str) -> list[str]:
    """Load the given CSVs into PostgreSQL with `load_csv.load_table` and return the file names loaded.

    `load_table` builds each table as ``<table>__loading`` and swaps it in whole, so a
    query during the load sees the old complete table and never a partial one.
    """
    import psycopg2  # imported here so the cube-only setup needs no driver

    from surimi_data_mcp.loader.load_csv import CSV_TO_TABLE, _ensure_schemas, load_table

    csv_paths = list(csv_paths)
    conn = psycopg2.connect(postgres_url)
    conn.autocommit = True
    cur = conn.cursor()
    # Create only the schemas that the tables of this batch live in.
    batch_tables = [CSV_TO_TABLE[p.name] for p in csv_paths if p.name in CSV_TO_TABLE]
    _ensure_schemas(cur, batch_tables)

    loaded: list[str] = []
    for path in csv_paths:
        fname = path.name
        table = CSV_TO_TABLE.get(fname)
        if table is None:
            # A CSV without a target table cannot be loaded.
            _log(f"skip Postgres load for {fname} (no CSV_TO_TABLE entry)")
            continue

        count, ncol, _ = load_table(cur, str(path), table)
        _log(f"postgres {table}: {count} rows loaded ({ncol} cols)")
        loaded.append(fname)

    cur.close()
    conn.close()
    return loaded


# ──────────────────────────────── bootstrap ─────────────────────────────────
def bootstrap(
    data_src: str,
    docs_src: str,
    cube_db: str,
    postgres_url: Optional[str] = None,
    analytic_datasets: Optional[Sequence[str]] = None,
    build_cube: bool = True,
    *,
    _pg_loader: Optional[Callable[[list[Path], str], list[str]]] = None,
    _tmp_root: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve the data and docs sources, build the cube, load PostgreSQL and return a summary.

    Args:
        data_src: data-lake root, a local directory or ``s3://bucket/prefix``.
        docs_src: docs root, a local directory or ``s3://bucket/prefix``.
        cube_db: path of the DuckDB cube file to build.
        postgres_url: PostgreSQL URL for the CSVs in `ANALYTIC_CSVS`. When None, PostgreSQL is skipped.
        analytic_datasets: CSV file names to load instead of `ANALYTIC_CSVS`.
        build_cube: when False the cube is not built and the file at `cube_db` is not
            written. The sources are still resolved and PostgreSQL is still loaded. The
            load-data Job sets this because the server pod pulls a prebuilt cube.
        _pg_loader: loader ``(csv_paths, url) -> [names]`` that replaces the PostgreSQL loader in tests.
        _tmp_root: directory for ``s3://`` downloads in tests. By default a new temporary directory is created and kept.

    Returns:
        A summary dict::

            {
              "cube_db": str,                 # the cube file path (may be unbuilt)
              "data_dir": str,                # resolved local data dir
              "datasets_built": [str, ...],   # dataset names in the cube
                                              #   (empty when build_cube=False)
              "rows_total": int,              # rows across all cube datasets
                                              #   (0 when build_cube=False)
              "cube_skipped": bool,           # True when build_cube=False
              "postgres_loaded": [str,...] | "skipped",
              "docs_dir": str,                # resolved local docs dir
            }

    Re-running over the same source replaces the cube and the PostgreSQL tables and does not append to them.
    """
    tmp_root = Path(_tmp_root) if _tmp_root else Path(tempfile.mkdtemp(prefix="surimi-bootstrap-"))

    # ── 1. resolve the sources ───────────────────────────────────────────────
    _log(f"resolving data source: {data_src}")
    data_dir = _resolve_source(data_src, tmp_root, "data")
    _log(f"resolving docs source: {docs_src}")
    docs_dir = _resolve_source(docs_src, tmp_root, "docs")

    # ── 2. build the cube, or skip it ────────────────────────────────────────
    if build_cube:
        _log(f"building cube from {data_dir} -> {cube_db}")
        cube = cube_mod.build_cube(data_dir, db_path=cube_db)
        registry = cube.registry()
        datasets_built = [entry["dataset"] for entry in registry]
        rows_total = sum(int(entry["row_count"]) for entry in registry)
        cube.close()
        cube_skipped = False
        _log(f"cube built: {len(datasets_built)} dataset(s), {rows_total} row(s)")
    else:
        # The server pod pulls a prebuilt cube, so this run leaves ``cube_db`` alone
        # and reports the skip in the summary.
        datasets_built = []
        rows_total = 0
        cube_skipped = True
        _log(f"build_cube=False -> SKIPPING cube build (prebuilt cube pulled "
             f"separately); {cube_db} left untouched")

    # ── 3. PostgreSQL, or skip ───────────────────────────────────────────────
    if postgres_url is None:
        postgres_loaded: Any = "skipped"
        _log("postgres_url is None -> skipping Postgres load (cube/docs topology)")
    else:
        analytic_paths = _select_analytic_csvs(data_dir, analytic_datasets)
        _log(f"loading {len(analytic_paths)} analytic CSV(s) into Postgres")
        loader = _pg_loader if _pg_loader is not None else _default_pg_loader
        postgres_loaded = list(loader(analytic_paths, postgres_url))

    # ── 4. docs: already a local directory, indexed later by docsearch ───────
    _log(f"docs dir resolved (index built lazily by docsearch): {docs_dir}")

    # ── 5. summary ───────────────────────────────────────────────────────────
    summary: dict[str, Any] = {
        "cube_db": cube_db,
        "data_dir": data_dir,
        "datasets_built": datasets_built,
        "rows_total": rows_total,
        "cube_skipped": cube_skipped,
        "postgres_loaded": postgres_loaded,
        "docs_dir": docs_dir,
    }
    _log(f"bootstrap complete: {summary}")
    return summary


# ──────────────────────────────── CLI entrypoint ────────────────────────────
def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable: 1, true, yes and on are True, anything else is False."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run `bootstrap` with settings from the environment and return the exit code.

    The chart's load-data Job runs ``python -u -m surimi_data_mcp.loader.bootstrap``
    with these variables:
      * SURIMI_DATA_SRC   - data lake source, a local directory or ``s3://bucket/prefix`` (required, exit code 2 when unset)
      * SURIMI_DOCS_SRC   - docs source, a local directory or ``s3://bucket/prefix``
      * SURIMI_CUBE_DB    - cube file path, ``/surimi/cube.duckdb`` when unset
      * DATABASE_URL      - PostgreSQL URL, PostgreSQL is skipped when unset
      * SURIMI_BUILD_CUBE - set to ``0`` or ``false`` to skip the cube build, which is on by default
    """
    data_src = os.environ.get("SURIMI_DATA_SRC", "")
    docs_src = os.environ.get("SURIMI_DOCS_SRC", "")
    cube_db = os.environ.get("SURIMI_CUBE_DB", "/surimi/cube.duckdb")
    postgres_url = os.environ.get("DATABASE_URL") or None
    build_cube = _env_bool("SURIMI_BUILD_CUBE", default=True)

    if not data_src:
        _log("ERROR: SURIMI_DATA_SRC is not set (data lake source required)")
        return 2

    summary = bootstrap(
        data_src=data_src,
        docs_src=docs_src,
        cube_db=cube_db,
        postgres_url=postgres_url,
        build_cube=build_cube,
    )
    _log(f"done. cube_skipped={summary['cube_skipped']} "
         f"postgres_loaded={summary['postgres_loaded']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
