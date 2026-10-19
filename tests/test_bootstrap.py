"""Tests for the surimi-mcp init step (`surimi_data_mcp/loader/bootstrap.py`, `bootstrap`).

What bootstrap does (the Helm chart's init step):
  1. Resolve the data + docs sources (local dir OR s3://bucket/prefix -> temp dir).
  2. Build the DuckDB cube from the data dir via the EXISTING cube.build_cube
     (loss-less; never reimplemented here).
  3. If a postgres_url is given, load ONLY the documented analytic subset of CSVs
     into Postgres via the existing `loader/load_csv.py` path. If None, skip Postgres.
  4. Resolve docs to a local dir and return its path (docsearch indexes lazily).
  5. Return a structured, idempotent summary.

Anti-corner-cutting rules enforced here (per the plan):
  * No tautologies: every count/sum is checked against an INDEPENDENT oracle
    (stdlib csv) over the same source text, not "the function returns what it
    returns".
  * The s3 path must PROVE a real download happened (object actually fetched
    from the mock bucket, then built), not a silent no-op.
  * postgres_url=None must SKIP Postgres cleanly and still build the cube.
  * The analytic subset is an explicit list; heavy raw (DATRAS-style) is in the
    cube but NOT flagged for Postgres.
  * Idempotent: a second run over the same source yields the same cube (oracle
    row count unchanged) and does not crash.
  * A missing/empty source yields a clear, honest result, never fabricated data.
"""
from __future__ import annotations

import csv as _csv
import os
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from surimi_data_mcp.loader import bootstrap as bootstrap_mod
from surimi_data_mcp.loader.bootstrap import ANALYTIC_CSVS, bootstrap


# ───────────────────────── temp corpus fixtures ─────────────────────────────
# Two analytic CSVs (in CSV_TO_TABLE) + one heavy-raw CSV (a DATRAS-style file
# NOT in CSV_TO_TABLE). All three must end up in the cube; only the two analytic
# ones may be flagged for Postgres.

AER_ECONOMIC_ROWS = [
    ["year", "country_code", "employment_fte", "gross.value.added"],
    ["2019", "ESP", "1000", "1234.5"],
    ["2019", "FRA", "500", "678.25"],
    ["2020", "ESP", "1100", "1500.0"],
    ["2020", "FRA", "550", "700.0"],
]

AER_CAPACITY_ROWS = [
    ["year", "country_code", "vessels", "total.kw"],
    ["2019", "ESP", "20", "5000"],
    ["2020", "ESP", "22", "5100"],
]

# Heavy raw: a DATRAS-style haul file. NOT in the analytic subset -> cube only.
DATRAS_ROWS = [
    ["haul_id", "species", "length_cm", "count"],
    ["H1", "COD", "32", "5"],
    ["H2", "HER", "18", "40"],
    ["H3", "COD", "41", "2"],
]


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        _csv.writer(f).writerows(rows)


def _oracle_rows(path: Path) -> list[dict[str, str]]:
    """Read a CSV the *second* way (stdlib csv) for an independent cross-check."""
    with open(path, newline="") as f:
        return list(_csv.DictReader(f))


# Map cube dataset name -> (relative source path, rows). The cube derives its
# logical dataset name from the sanitized CSV file STEM (cube._sanitize_name),
# so we compute the key that way instead of guessing it, the test then tracks
# the real cube behaviour and cannot pass by a hand-copied coincidence. The
# analytic file names are the EXISTING CSV_TO_TABLE keys; the heavy-raw file is
# deliberately NOT one of them.
from surimi_data_mcp.engine.cube import _sanitize_name as _cube_name  # noqa: E402

_ANALYTIC_ECON_CSV = "hive_eu_dcf_aer_economic_2013_2023.csv"
_ANALYTIC_CAP_CSV = "hive_eu_dcf_aer_capacity.csv"
_RAW_DATRAS_CSV = "datras_hauls.csv"  # NOT in CSV_TO_TABLE -> cube only


def _data_layout() -> dict[str, tuple[str, list[list[str]]]]:
    files = [
        (_ANALYTIC_ECON_CSV, _ANALYTIC_ECON_CSV, AER_ECONOMIC_ROWS),
        (_ANALYTIC_CAP_CSV, _ANALYTIC_CAP_CSV, AER_CAPACITY_ROWS),
        (_RAW_DATRAS_CSV, f"9_RawSpatial/{_RAW_DATRAS_CSV}", DATRAS_ROWS),
    ]
    # Key by the cube's own logical name (sanitized file stem).
    return {_cube_name(Path(rel).stem): (rel, rows) for (_fname, rel, rows) in files}


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A temp data lake with two analytic CSVs and one heavy-raw CSV."""
    root = tmp_path / "lake"
    for _name, (rel, rows) in _data_layout().items():
        _write_csv(root / rel, rows)
    return root


@pytest.fixture
def docs_dir(tmp_path: Path) -> Path:
    """A temp docs dir (markdown), bootstrap resolves but does NOT index it."""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "readme.md").write_text(
        "# SURIMI fisheries\n\nThe AER economic data covers EU member states.\n",
        encoding="utf-8",
    )
    return root


# ════════════════════════════════ TESTS ═════════════════════════════════════

# ── (a) local-dir source ─────────────────────────────────────────────────────
def test_local_dir_builds_queryable_cube(data_dir: Path, docs_dir: Path):
    """Local source: cube built, queryable; datasets_built == input files;
    rows match an independent oracle count."""
    cube_db = str(data_dir.parent / "cube.duckdb")
    summary = bootstrap(
        data_src=str(data_dir),
        docs_src=str(docs_dir),
        cube_db=cube_db,
    )

    # datasets_built lists exactly the three CSVs (sanitized logical names =
    # the cube's own naming of the file stems).
    assert set(summary["datasets_built"]) == set(_data_layout().keys())

    # The cube is actually queryable and counts match an INDEPENDENT oracle.
    from surimi_data_mcp.engine import cube as cube_mod
    c = cube_mod.build_cube(str(data_dir), db_path=cube_db)
    for name, (rel, _rows) in _data_layout().items():
        got = c.query(f'SELECT COUNT(*) AS n FROM "{name}"')[0]["n"]
        oracle = len(_oracle_rows(data_dir / rel))
        assert got == oracle, f"{name}: cube {got} != oracle {oracle}"
    c.close()

    # rows_total is the honest sum across datasets (oracle-checked).
    oracle_total = sum(len(_oracle_rows(data_dir / rel)) for rel, _ in _data_layout().values())
    assert summary["rows_total"] == oracle_total

    # A real aggregate is computable through the built cube (numbers from engine).
    econ_name = _cube_name(Path(_ANALYTIC_ECON_CSV).stem)
    c2 = cube_mod.build_cube(str(data_dir), db_path=cube_db)
    got_sum = c2.query(f'SELECT SUM(employment_fte) AS s FROM "{econ_name}"')[0]["s"]
    oracle_sum = sum(int(r["employment_fte"]) for r in _oracle_rows(data_dir / _ANALYTIC_ECON_CSV))
    assert got_sum == oracle_sum == (1000 + 500 + 1100 + 550)
    c2.close()


def test_local_dir_returns_resolved_docs_dir(data_dir: Path, docs_dir: Path):
    """Docs are resolved to a local dir and returned (so the MCP can point
    SURIMI_DOCS_DIRS at it). bootstrap does NOT rebuild the index here."""
    cube_db = str(data_dir.parent / "cube.duckdb")
    summary = bootstrap(data_src=str(data_dir), docs_src=str(docs_dir), cube_db=cube_db)
    assert Path(summary["docs_dir"]).is_dir()
    # The returned dir actually contains the source doc (resolution, not fabrication).
    names = {p.name for p in Path(summary["docs_dir"]).rglob("*") if p.is_file()}
    assert "readme.md" in names


# ── (b) s3:// source (mock S3 via moto) ──────────────────────────────────────
@mock_aws
def test_s3_source_downloads_and_builds_same_as_local(tmp_path: Path, docs_dir: Path):
    """An s3:// data_src downloads the prefix and builds the SAME cube as local.

    Proves a real download (the objects exist ONLY in the mock bucket; the cube
    can only be built if they were fetched to disk first)."""
    bucket = "surimi-lake"
    prefix = "surimi-data"
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=bucket)

    # Put the same three CSVs into the fake bucket (only there, no local copy).
    expected_counts: dict[str, int] = {}
    for name, (rel, rows) in _data_layout().items():
        body = "".join(",".join(c) + "\n" for c in rows).encode("utf-8")
        s3.put_object(Bucket=bucket, Key=f"{prefix}/{rel}", Body=body)
        expected_counts[name] = len(rows) - 1  # minus header

    cube_db = str(tmp_path / "cube_s3.duckdb")
    summary = bootstrap(
        data_src=f"s3://{bucket}/{prefix}",
        docs_src=str(docs_dir),
        cube_db=cube_db,
    )

    assert set(summary["datasets_built"]) == set(expected_counts.keys())
    assert summary["rows_total"] == sum(expected_counts.values())

    # Build over the downloaded copy and confirm row counts match what we put in
    # the bucket (independent oracle = the bytes we uploaded).
    from surimi_data_mcp.engine import cube as cube_mod
    c = cube_mod.build_cube(summary["data_dir"], db_path=cube_db)
    for name, n in expected_counts.items():
        got = c.query(f'SELECT COUNT(*) AS cnt FROM "{name}"')[0]["cnt"]
        assert got == n, f"{name}: built {got} from S3 download != uploaded {n}"
    c.close()


@mock_aws
def test_s3_download_is_not_a_noop(tmp_path: Path, docs_dir: Path):
    """The s3 path MUST physically write files locally (prove it, don't trust it).

    If bootstrap had quietly skipped the download, data_dir would be empty and
    the cube would have zero datasets, this asserts the opposite."""
    bucket = "surimi-lake-2"
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=bucket)
    rows = AER_CAPACITY_ROWS
    body = "".join(",".join(c) + "\n" for c in rows).encode("utf-8")
    s3.put_object(Bucket=bucket, Key="p/hive_eu_dcf_aer_capacity.csv", Body=body)

    cube_db = str(tmp_path / "cube_noop.duckdb")
    summary = bootstrap(data_src=f"s3://{bucket}/p", docs_src=str(docs_dir), cube_db=cube_db)

    data_dir = Path(summary["data_dir"])
    # A real file landed on disk (the download is observable on the filesystem).
    local_csvs = list(data_dir.rglob("*.csv"))
    assert local_csvs, "s3 download produced no local CSV files (no-op)"
    assert any(p.name == "hive_eu_dcf_aer_capacity.csv" for p in local_csvs)
    assert summary["datasets_built"] == [_cube_name("hive_eu_dcf_aer_capacity")]


# ── (c) postgres_url=None -> skip Postgres cleanly ───────────────────────────
def test_postgres_none_skips_cleanly(data_dir: Path, docs_dir: Path):
    """With postgres_url=None, Postgres is SKIPPED and the cube still builds."""
    cube_db = str(data_dir.parent / "cube.duckdb")
    summary = bootstrap(
        data_src=str(data_dir),
        docs_src=str(docs_dir),
        cube_db=cube_db,
        postgres_url=None,
    )
    # The summary states the skip explicitly (honest, not an empty success).
    assert summary["postgres_loaded"] == "skipped"
    # The cube was still built (the other topology works without Postgres).
    assert set(summary["datasets_built"]) == set(_data_layout().keys())
    assert summary["rows_total"] > 0


# ── (d) analytic-subset selection ────────────────────────────────────────────
def test_analytic_subset_excludes_heavy_raw(data_dir: Path, docs_dir: Path):
    """Only the documented analytic CSVs are flagged for Postgres; the heavy raw
    DATRAS-style file is in the cube but NOT in the Postgres-load list."""
    cube_db = str(data_dir.parent / "cube.duckdb")
    # Force the Postgres branch WITHOUT a live DB by passing a no-op loader. The
    # bootstrap must hand the loader exactly the analytic CSV paths and nothing
    # else, so we capture them.
    captured: list[Path] = []

    def fake_pg_load(csv_paths, postgres_url):
        captured.extend(Path(p) for p in csv_paths)
        return [Path(p).name for p in csv_paths]

    summary = bootstrap(
        data_src=str(data_dir),
        docs_src=str(docs_dir),
        cube_db=cube_db,
        postgres_url="postgresql://unused",
        analytic_datasets=None,
        _pg_loader=fake_pg_load,
    )

    loaded_names = {p.name for p in captured}
    # The two analytic CSVs are present.
    assert "hive_eu_dcf_aer_economic_2013_2023.csv" in loaded_names
    assert "hive_eu_dcf_aer_capacity.csv" in loaded_names
    # The heavy-raw DATRAS file is NOT handed to Postgres.
    assert not any("datras" in n for n in loaded_names)

    # But the heavy-raw file IS in the cube (cube is loss-less / superset).
    assert "datras_hauls" in summary["datasets_built"]
    assert summary["postgres_loaded"] == ["hive_eu_dcf_aer_economic_2013_2023.csv",
                                          "hive_eu_dcf_aer_capacity.csv"] or \
        set(summary["postgres_loaded"]) == loaded_names


def test_analytic_subset_constant_matches_loader(data_dir: Path):
    """ANALYTIC_CSVS is derived from the EXISTING loader's CSV_TO_TABLE (single
    source of truth), it cannot drift from what load_csv actually loads."""
    from surimi_data_mcp.loader.load_csv import CSV_TO_TABLE
    assert set(ANALYTIC_CSVS) == set(CSV_TO_TABLE.keys())
    # And a heavy-raw file is definitively NOT in the analytic subset.
    assert "datras_hauls.csv" not in ANALYTIC_CSVS


def test_explicit_analytic_datasets_override(data_dir: Path, docs_dir: Path):
    """A caller may pass an explicit analytic_datasets list; only those present
    on disk get flagged for Postgres."""
    cube_db = str(data_dir.parent / "cube.duckdb")
    captured: list[str] = []

    def fake_pg_load(csv_paths, postgres_url):
        captured.extend(Path(p).name for p in csv_paths)
        return [Path(p).name for p in csv_paths]

    summary = bootstrap(
        data_src=str(data_dir),
        docs_src=str(docs_dir),
        cube_db=cube_db,
        postgres_url="postgresql://unused",
        analytic_datasets=["hive_eu_dcf_aer_capacity.csv"],
        _pg_loader=fake_pg_load,
    )
    assert captured == ["hive_eu_dcf_aer_capacity.csv"]


# ── (e) idempotent ───────────────────────────────────────────────────────────
def test_idempotent_double_run(data_dir: Path, docs_dir: Path):
    """Running bootstrap twice over the same source yields the same cube (no
    duplicate rows; oracle-checked) and does not crash."""
    cube_db = str(data_dir.parent / "cube.duckdb")

    s1 = bootstrap(data_src=str(data_dir), docs_src=str(docs_dir), cube_db=cube_db)
    s2 = bootstrap(data_src=str(data_dir), docs_src=str(docs_dir), cube_db=cube_db)

    assert s1["datasets_built"] == s2["datasets_built"]
    assert s1["rows_total"] == s2["rows_total"]

    # Independent oracle: the per-dataset counts after the SECOND run still equal
    # the source-file counts (no doubling, no loss).
    from surimi_data_mcp.engine import cube as cube_mod
    c = cube_mod.build_cube(str(data_dir), db_path=cube_db)
    for name, (rel, _rows) in _data_layout().items():
        got = c.query(f'SELECT COUNT(*) AS n FROM "{name}"')[0]["n"]
        oracle = len(_oracle_rows(data_dir / rel))
        assert got == oracle, f"after 2 runs {name}: {got} != {oracle}"
    c.close()


# ── (f) missing/empty source ─────────────────────────────────────────────────
def test_missing_data_source_is_honest(tmp_path: Path, docs_dir: Path):
    """A non-existent data dir yields an empty-but-valid result (no fabricated
    datasets), never a silent fake success."""
    missing = tmp_path / "does_not_exist"
    cube_db = str(tmp_path / "cube_missing.duckdb")
    summary = bootstrap(data_src=str(missing), docs_src=str(docs_dir), cube_db=cube_db)
    assert summary["datasets_built"] == []
    assert summary["rows_total"] == 0
    # The cube file is still a valid, queryable (empty) cube.
    from surimi_data_mcp.engine import cube as cube_mod
    c = cube_mod.build_cube(str(missing), db_path=cube_db)
    assert c.datasets() == []
    assert c.query("SELECT 1 AS one")[0]["one"] == 1
    c.close()


def test_empty_data_dir_is_honest(tmp_path: Path, docs_dir: Path):
    """An existing-but-empty data dir also yields zero datasets, no crash."""
    empty = tmp_path / "empty"
    empty.mkdir()
    cube_db = str(tmp_path / "cube_empty.duckdb")
    summary = bootstrap(data_src=str(empty), docs_src=str(docs_dir), cube_db=cube_db)
    assert summary["datasets_built"] == []
    assert summary["rows_total"] == 0


def test_summary_shape_is_complete(data_dir: Path, docs_dir: Path):
    """The returned summary carries every documented field."""
    cube_db = str(data_dir.parent / "cube.duckdb")
    summary = bootstrap(data_src=str(data_dir), docs_src=str(docs_dir), cube_db=cube_db)
    for key in ("cube_db", "datasets_built", "rows_total", "postgres_loaded",
                "docs_dir", "data_dir"):
        assert key in summary, f"summary missing {key!r}"
    assert summary["cube_db"] == cube_db


# ── (g) build_cube=False -> skip cube, still load Postgres ───────────────────
# The deployed topology splits the work: the SERVER pod's initContainer pulls a
# PREBUILT cube + docs from MinIO (no rebuild in-pod), while the load-data Job
# loads ONLY the analytic subset into Postgres. So the Job calls bootstrap with
# build_cube=False: it must SKIP the (slow, multi-GB) DuckDB build but STILL
# resolve the sources and load Postgres. These are the failing-first tests for
# that param.

def test_build_cube_false_skips_cube_but_loads_postgres(data_dir: Path, docs_dir: Path):
    """build_cube=False: NO cube is built (no .duckdb file written, datasets
    reported as skipped) BUT Postgres is still attempted with the analytic
    subset."""
    cube_db = str(data_dir.parent / "should_not_be_written.duckdb")
    captured: list[Path] = []

    def fake_pg_load(csv_paths, postgres_url):
        captured.extend(Path(p) for p in csv_paths)
        return [Path(p).name for p in csv_paths]

    summary = bootstrap(
        data_src=str(data_dir),
        docs_src=str(docs_dir),
        cube_db=cube_db,
        postgres_url="postgresql://unused",
        build_cube=False,
        _pg_loader=fake_pg_load,
    )

    # The cube file was NOT written (the build was genuinely skipped, not built
    # then deleted, it must never have been created).
    assert not Path(cube_db).exists(), "cube_db was written despite build_cube=False"

    # The summary states the cube was skipped explicitly (honest, not a fake
    # empty success that looks identical to a real empty cube).
    assert summary["datasets_built"] == []
    assert summary["rows_total"] == 0
    assert summary["cube_skipped"] is True

    # Postgres was STILL attempted with exactly the analytic CSVs (the heavy-raw
    # DATRAS file is excluded; the two analytic CSVs are present).
    loaded_names = {p.name for p in captured}
    assert "hive_eu_dcf_aer_economic_2013_2023.csv" in loaded_names
    assert "hive_eu_dcf_aer_capacity.csv" in loaded_names
    assert not any("datras" in n for n in loaded_names)
    assert set(summary["postgres_loaded"]) == loaded_names


def test_build_cube_true_is_the_default(data_dir: Path, docs_dir: Path):
    """The default (build_cube unspecified) still BUILDS the cube, the existing
    server-rebuild topology must not regress."""
    cube_db = str(data_dir.parent / "cube_default.duckdb")
    summary = bootstrap(data_src=str(data_dir), docs_src=str(docs_dir), cube_db=cube_db)
    assert Path(cube_db).exists()
    assert set(summary["datasets_built"]) == set(_data_layout().keys())
    assert summary["rows_total"] > 0
    assert summary.get("cube_skipped", False) is False


def test_build_cube_false_still_resolves_s3_and_docs(tmp_path: Path, docs_dir: Path):
    """build_cube=False over an s3:// data_src STILL downloads the data (so the
    Job can read the analytic CSVs) and resolves docs, even though it skips the
    cube build."""

    @mock_aws
    def run():
        bucket = "surimi-lake-nocube"
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=bucket)
        rows = AER_CAPACITY_ROWS
        body = "".join(",".join(c) + "\n" for c in rows).encode("utf-8")
        s3.put_object(Bucket=bucket, Key="p/hive_eu_dcf_aer_capacity.csv", Body=body)

        captured: list[Path] = []

        def fake_pg_load(csv_paths, postgres_url):
            captured.extend(Path(p) for p in csv_paths)
            return [Path(p).name for p in csv_paths]

        cube_db = str(tmp_path / "nocube.duckdb")
        summary = bootstrap(
            data_src=f"s3://{bucket}/p",
            docs_src=str(docs_dir),
            cube_db=cube_db,
            postgres_url="postgresql://unused",
            build_cube=False,
            _pg_loader=fake_pg_load,
        )
        # No cube file, but the data WAS downloaded (proving resolution ran).
        assert not Path(cube_db).exists()
        data_dir = Path(summary["data_dir"])
        local_csvs = list(data_dir.rglob("*.csv"))
        assert any(p.name == "hive_eu_dcf_aer_capacity.csv" for p in local_csvs)
        # Postgres got the downloaded analytic CSV.
        assert [p.name for p in captured] == ["hive_eu_dcf_aer_capacity.csv"]
        # docs still resolved.
        assert Path(summary["docs_dir"]).is_dir()

    run()


def test_build_cube_false_summary_shape(data_dir: Path, docs_dir: Path):
    """The build_cube=False summary still carries every documented field plus the
    cube_skipped marker."""
    cube_db = str(data_dir.parent / "nocube_shape.duckdb")
    summary = bootstrap(
        data_src=str(data_dir),
        docs_src=str(docs_dir),
        cube_db=cube_db,
        postgres_url="postgresql://unused",
        build_cube=False,
        _pg_loader=lambda paths, url: [Path(p).name for p in paths],
    )
    for key in ("cube_db", "datasets_built", "rows_total", "postgres_loaded",
                "docs_dir", "data_dir", "cube_skipped"):
        assert key in summary, f"summary missing {key!r}"
