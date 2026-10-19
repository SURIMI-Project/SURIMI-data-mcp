"""Tests for the embedded-DuckDB cube builder (`surimi_data_mcp/engine/cube.py`, `build_cube`).

The cube converts data-lake CSVs into a queryable DuckDB database (one table
per logical dataset) plus a registry mapping logical name -> columns + types.

Correctness rules these tests enforce (per the plan, anti-corner-cutting):
  * Type inference is COMPUTED, not asserted tautologically: numeric vs text is
    checked against what a CSV actually contains.
  * Numbers are computed by DuckDB and proven against an INDEPENDENT oracle:
    each aggregation test recomputes the expected value a SECOND way with the
    stdlib `csv` module over the same source file and asserts equality.
  * The registry is read back FROM the cube (information_schema), so it cannot
    drift from the tables that actually exist.
  * Reuses the existing `_sanitize_headers` for dotted/odd headers.
"""
from __future__ import annotations

import csv as _csv
from pathlib import Path

import pytest

from surimi_data_mcp.engine import cube as cube_mod
from surimi_data_mcp.engine.cube import BUILD_SCHEMA, build_cube
from surimi_data_mcp.loader.load_csv import _sanitize_headers


# ───────────────────────── temp corpus fixtures ─────────────────────────────
# A small, hand-authored corpus. Values are chosen so an independent oracle can
# recompute every asserted aggregate from the same CSV text.

LANDINGS_ROWS = [
    # year, area, species, landings_tonnes  (landings has a fractional value
    # in one row so DOUBLE inference is exercised; year/area/species are text-
    # or-int identifiers)
    ["year", "area", "species", "landings_tonnes"],
    ["2019", "27.4", "COD", "100.5"],
    ["2019", "27.4", "HER", "50.0"],
    ["2020", "27.4", "COD", "200.0"],
    ["2020", "37.1", "HAK", "75.25"],
    ["2021", "37.1", "HAK", "10.0"],
]

# Odd/dotted headers + spaces + mixed case + a duplicate-after-sanitize pair.
ODD_ROWS = [
    ["total.trips", "Mean age of vessels", "Vessel.Length.Category", "total_trips"],
    ["12", "20.5", "VL1218", "999"],
    ["7", "15.0", "VL2440", "111"],
]


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    with open(path, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerows(rows)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A temp data dir with two CSVs in nested category folders (like the lake)."""
    root = tmp_path / "lake"
    (root / "1_Fisheries").mkdir(parents=True)
    (root / "9_Misc").mkdir(parents=True)
    _write_csv(root / "1_Fisheries" / "landings.csv", LANDINGS_ROWS)
    _write_csv(root / "9_Misc" / "odd headers.csv", ODD_ROWS)
    return root


# ─────────────────────────── independent oracle ─────────────────────────────

def _oracle_rows(path: Path) -> list[dict[str, str]]:
    """Read a CSV the *second* way (stdlib csv -> list of dict) for cross-check."""
    with open(path, newline="") as f:
        return list(_csv.DictReader(f))


# ════════════════════════════════ TESTS ═════════════════════════════════════

def test_builds_from_temp_dir(corpus: Path):
    """A cube builds from a temp dir without crashing and reports its datasets."""
    c = build_cube(str(corpus))
    names = set(c.datasets())
    # Both CSVs become datasets; logical names are the sanitized file stems.
    assert "landings" in names
    assert "odd_headers" in names
    assert len(names) == 2


def test_known_dataset_is_queryable(corpus: Path):
    """A known dataset can be SELECTed and returns the full row set."""
    c = build_cube(str(corpus))
    n = c.query("SELECT COUNT(*) AS n FROM landings")[0]["n"]
    # Oracle: count data rows in the source CSV.
    oracle_n = len(_oracle_rows(corpus / "1_Fisheries" / "landings.csv"))
    assert n == oracle_n == 5


def test_numeric_vs_text_type_inference(corpus: Path):
    """Numeric columns are numeric in the cube; identifier columns stay text."""
    c = build_cube(str(corpus))
    cols = {col["name"]: col["type"] for col in c.columns("landings")}

    # landings_tonnes has fractional values -> a numeric (DOUBLE) type.
    assert "DOUBLE" in cols["landings_tonnes"].upper() or "FLOAT" in cols["landings_tonnes"].upper()
    # year is all integers -> an integer type, NOT text.
    assert "INT" in cols["year"].upper()
    # species is non-numeric -> text.
    assert "VARCHAR" in cols["species"].upper() or "TEXT" in cols["species"].upper()

    # Prove "numeric is numeric" behaviourally: SUM works without a CAST and the
    # result equals an independent pandas-free hand sum over the same CSV.
    got = c.query("SELECT SUM(landings_tonnes) AS s FROM landings")[0]["s"]
    oracle = sum(float(r["landings_tonnes"]) for r in _oracle_rows(corpus / "1_Fisheries" / "landings.csv"))
    assert got == pytest.approx(oracle)
    assert got == pytest.approx(100.5 + 50.0 + 200.0 + 75.25 + 10.0)

    # And AVG over a GROUP BY equals the oracle group-wise mean (completeness:
    # every group present).
    grouped = {row["year"]: row["avg_l"]
               for row in c.query(
                   "SELECT year, AVG(landings_tonnes) AS avg_l FROM landings GROUP BY year ORDER BY year")}
    oracle_groups: dict = {}
    for r in _oracle_rows(corpus / "1_Fisheries" / "landings.csv"):
        oracle_groups.setdefault(int(r["year"]), []).append(float(r["landings_tonnes"]))
    assert set(grouped.keys()) == set(oracle_groups.keys())
    for y, vals in oracle_groups.items():
        assert grouped[y] == pytest.approx(sum(vals) / len(vals))


def test_dotted_odd_headers_sanitized(corpus: Path):
    """Dotted/spaced/mixed-case headers are sanitized via the existing helper."""
    c = build_cube(str(corpus))
    got_cols = [col["name"] for col in c.columns("odd_headers")]

    # Expectation comes from the SAME helper the loader uses (not a literal copy),
    # so the test cannot pass by coincidence and tracks the helper's behaviour.
    raw_header = ODD_ROWS[0]
    expected = _sanitize_headers(raw_header)
    assert got_cols == expected

    # Concretely: "total.trips" and "total_trips" both sanitise to total_trips,
    # so the collision must be disambiguated (total_trips + total_trips_2),
    # never silently merged/dropped.
    assert "total_trips" in got_cols
    assert "total_trips_2" in got_cols
    assert "mean_age_of_vessels" in got_cols
    assert "vessel_length_category" in got_cols
    # No raw dotted identifier survives.
    assert not any("." in name for name in got_cols)

    # The sanitized columns are actually addressable in SQL (bare identifiers).
    val = c.query("SELECT total_trips, total_trips_2 FROM odd_headers ORDER BY total_trips")[0]
    assert val["total_trips"] == 7
    assert val["total_trips_2"] == 111


def test_missing_dir_yields_empty_but_valid_cube(tmp_path: Path):
    """A non-existent data dir gives an empty-but-valid cube, not a crash."""
    missing = tmp_path / "does_not_exist"
    c = build_cube(str(missing))
    assert c.datasets() == []
    # Querying the empty cube's registry is still valid (honest empty).
    assert c.registry() == []
    # And the connection is live (a trivial query works).
    assert c.query("SELECT 1 AS one")[0]["one"] == 1


def test_empty_dir_yields_empty_but_valid_cube(tmp_path: Path):
    """An existing-but-empty dir also yields an empty cube (no fabricated tables)."""
    empty = tmp_path / "empty"
    empty.mkdir()
    c = build_cube(str(empty))
    assert c.datasets() == []
    assert c.registry() == []


def test_rebuild_is_idempotent(corpus: Path):
    """Rebuilding from the same corpus yields the same registry and same numbers.

    Idempotency must hold both for a fresh in-memory cube and for re-running
    build over an EXISTING db file (no duplicate rows, no DROP-then-stale state).
    """
    db_path = str(corpus.parent / "cube.duckdb")

    c1 = build_cube(str(corpus), db_path=db_path)
    reg1 = c1.registry()
    sum1 = c1.query("SELECT SUM(landings_tonnes) AS s FROM landings")[0]["s"]
    count1 = c1.query("SELECT COUNT(*) AS n FROM landings")[0]["n"]
    c1.close()

    # Rebuild into the SAME file: must not double the rows or change the schema.
    c2 = build_cube(str(corpus), db_path=db_path)
    reg2 = c2.registry()
    sum2 = c2.query("SELECT SUM(landings_tonnes) AS s FROM landings")[0]["s"]
    count2 = c2.query("SELECT COUNT(*) AS n FROM landings")[0]["n"]
    c2.close()

    assert reg1 == reg2
    assert count1 == count2 == 5
    assert sum1 == sum2

    # Independent oracle confirms idempotency didn't silently lose/duplicate rows.
    oracle_n = len(_oracle_rows(corpus / "1_Fisheries" / "landings.csv"))
    assert count2 == oracle_n


def test_registry_lists_datasets_and_columns_read_from_cube(corpus: Path):
    """The registry is read back FROM the cube and cannot drift from the tables.

    Proof of no-drift: every (dataset, column) the registry advertises must be
    queryable, and the set of columns must equal what DuckDB itself reports for
    that table (information_schema), recomputed here independently.
    """
    c = build_cube(str(corpus))
    reg = c.registry()

    # Registry shape matches BUILD_SCHEMA's promise.
    assert isinstance(reg, list)
    by_name = {entry["dataset"]: entry for entry in reg}
    assert set(by_name.keys()) == set(c.datasets())

    for name, entry in by_name.items():
        assert entry["source"]  # provenance: where it came from
        assert isinstance(entry["row_count"], int)
        reg_cols = [(col["name"], col["type"]) for col in entry["columns"]]

        # Independent recomputation of the truth straight from DuckDB's catalog,
        # NOT from the registry object under test.
        truth = c.query(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_name = '{name}' ORDER BY ordinal_position")
        truth_cols = [(r["column_name"], r["data_type"]) for r in truth]
        assert reg_cols == truth_cols

        # Every advertised column is actually selectable (cannot drift / lie).
        col_list = ", ".join(col["name"] for col in entry["columns"])
        rows = c.query(f"SELECT {col_list} FROM {name} LIMIT 1")
        assert set(rows[0].keys()) == {col["name"] for col in entry["columns"]}

    # row_count in the registry equals an independent COUNT(*) and the CSV oracle.
    landings_entry = by_name["landings"]
    assert landings_entry["row_count"] == c.query("SELECT COUNT(*) AS n FROM landings")[0]["n"]
    assert landings_entry["row_count"] == len(
        _oracle_rows(corpus / "1_Fisheries" / "landings.csv"))


def test_build_schema_describes_registry(corpus: Path):
    """BUILD_SCHEMA documents the registry shape and matches a real build."""
    assert isinstance(BUILD_SCHEMA, dict)
    # The schema names the registry entry fields so callers (and the MCP tool)
    # can rely on a stable contract.
    fields = BUILD_SCHEMA["registry_entry"]
    for key in ("dataset", "source", "row_count", "columns"):
        assert key in fields
    # column sub-shape documented too.
    for key in ("name", "type"):
        assert key in BUILD_SCHEMA["column_entry"]

    # The documented contract holds against a real registry entry.
    c = build_cube(str(corpus))
    entry = c.registry()[0]
    assert set(fields).issubset(set(entry.keys()))
    assert set(BUILD_SCHEMA["column_entry"]).issubset(set(entry["columns"][0].keys()))


def test_non_utf8_latin1_csv_loads_completely(tmp_path: Path):
    """A latin-1 (non-UTF-8) CSV loads with NO rows dropped.

    The real lake ships ISO-8859-1 files (accented species/area names). DuckDB's
    default UTF-8 read raises on them; the cube must fall back to a loss-free
    encoding (latin-1) rather than skip rows (which would corrupt every
    aggregate). Proven against an independent latin-1 csv read of the same file.
    """
    root = tmp_path / "lake"
    root.mkdir()
    p = root / "accented.csv"
    # 'Ã±' as a raw latin-1 byte (0xF1) is invalid UTF-8; write bytes directly.
    rows = [
        b"area,species,landings\n",
        b"27.4,Mu\xf1oz cod,100\n",   # 0xf1 = latin-1 small n-tilde
        b"27.4,plain hake,50\n",
        b"37.1,Mu\xf1oz cod,25\n",
    ]
    with open(p, "wb") as f:
        f.writelines(rows)

    c = build_cube(str(root))
    # All 3 data rows present (no silent drop).
    n = c.query("SELECT COUNT(*) AS n FROM accented")[0]["n"]
    assert n == 3
    # Numeric column still aggregates exactly; oracle = sum over latin-1 read.
    got = c.query("SELECT SUM(landings) AS s FROM accented")[0]["s"]
    oracle = sum(int(r["landings"])
                 for r in _csv.DictReader(open(p, encoding="latin-1", newline="")))
    assert got == oracle == 175
    # The accented value round-trips (not mangled to a replacement char).
    species = {row["species"] for row in c.query("SELECT DISTINCT species FROM accented")}
    assert any("\xf1" in s for s in species)


def test_late_appearing_text_does_not_break_type_inference(tmp_path: Path):
    """A column that looks numeric for thousands of rows then turns text loads.

    DuckDB samples the first N rows to guess types. The real DATRAS files have a
    ``Sex`` column that is ``-9`` (int) for tens of thousands of rows then ``F``
    far down, a sampled guess of BIGINT then throws on the first text value and
    drops the load. The cube must scan the WHOLE file for inference so the
    column is text and every row loads. Proven: row count == oracle, and the
    text value is present.
    """
    root = tmp_path / "lake"
    root.mkdir()
    p = root / "sexcol.csv"
    rows = [["id", "sex", "len"]]
    # 30k rows where sex looks like an int (-9), then one genuine text value.
    for i in range(30000):
        rows.append([str(i), "-9", "12"])
    rows.append([str(30000), "F", "20"])
    _write_csv(p, rows)

    c = build_cube(str(root))
    n = c.query("SELECT COUNT(*) AS n FROM sexcol")[0]["n"]
    assert n == 30001  # nothing dropped
    assert n == len(_oracle_rows(p))
    # sex is text (so the late "F" is retained, not a conversion failure).
    sex_type = {col["name"]: col["type"] for col in c.columns("sexcol")}["sex"]
    assert "VARCHAR" in sex_type.upper() or "TEXT" in sex_type.upper()
    has_f = c.query("SELECT COUNT(*) AS n FROM sexcol WHERE sex = 'F'")[0]["n"]
    assert has_f == 1
    # len is still numeric and aggregates exactly vs oracle.
    got = c.query("SELECT SUM(len) AS s FROM sexcol")[0]["s"]
    oracle = sum(int(r["len"]) for r in _oracle_rows(p))
    assert got == oracle == 30000 * 12 + 20


def test_windows1252_csv_transcodes_and_loads_completely(tmp_path: Path):
    """A windows-1252 CSV (bytes DuckDB's latin-1 reader rejects) loads fully.

    The real lake's ICES division files use cp1252 (curly quotes / 0x80-0x9f).
    DuckDB's native latin-1 reader rejects those bytes, so the cube transcodes
    the file to UTF-8 (loss-free) and loads every row. Proven against an
    independent cp1252 csv read of the same bytes.
    """
    root = tmp_path / "lake"
    root.mkdir()
    p = root / "cp1252.csv"
    # 0x92 = cp1252 right single quote (invalid in strict latin-1 for DuckDB),
    # 0x96 = en dash. Both are real bytes seen in the lake.
    with open(p, "wb") as f:
        f.write(b"area,note,catch\n")
        f.write(b"27.4,O\x92Brien bank,100\n")     # 0x92 apostrophe
        f.write(b"37.1,north\x96south split,55\n")  # 0x96 dash
    c = build_cube(str(root))
    n = c.query("SELECT COUNT(*) AS n FROM cp1252")[0]["n"]
    assert n == 2
    got = c.query("SELECT SUM(catch) AS s FROM cp1252")[0]["s"]
    oracle = sum(int(r["catch"])
                 for r in _csv.DictReader(open(p, encoding="cp1252", newline="")))
    assert got == oracle == 155


def test_collision_headers_not_silently_dropped(tmp_path: Path):
    """A header collision keeps BOTH columns (no silent data loss)."""
    root = tmp_path / "lake"
    root.mkdir()
    _write_csv(root / "dup.csv", [
        ["a.b", "a_b", "a b"],   # all three sanitise toward a_b
        ["1", "2", "3"],
    ])
    c = build_cube(str(root))
    cols = [col["name"] for col in c.columns("dup")]
    assert len(cols) == 3  # no column lost to collision
    assert len(set(cols)) == 3  # all unique after disambiguation


# ═══════════ prebuilt-cube topology: open, don't rebuild (deploy-critical) ════

def test_get_cube_opens_prebuilt_without_rebuilding(tmp_path, monkeypatch):
    """The deployed pod pulls a prebuilt cube.duckdb and must OPEN it, NOT rebuild
    from SURIMI_DATA_DIR (which it does not have). Regression: get_cube() rebuilt."""
    from surimi_data_mcp.engine import cube as cube_module
    src = tmp_path / "src"; src.mkdir()
    (src / "t.csv").write_text("a,b\n1,10\n2,20\n", encoding="utf-8")
    prebuilt = tmp_path / "pre.duckdb"
    cube_module.build_cube(str(src), db_path=str(prebuilt))
    empty = tmp_path / "empty"; empty.mkdir()           # the pod has NO data dir
    monkeypatch.setenv("SURIMI_CUBE_DB", str(prebuilt))
    monkeypatch.setenv("SURIMI_DATA_DIR", str(empty))
    monkeypatch.setattr(cube_module, "_CUBE", None, raising=False)
    c = cube_module.get_cube()
    assert "t" in c.datasets()                          # opened prebuilt, not the empty dir
    assert c.query('SELECT sum(b) AS s FROM "t"')[0]["s"] == 30
    # the prebuilt provenance must SURVIVE -> proves it was OPENED, not rebuilt
    # from the empty data dir (which wipes _cube_sources).
    reg = {r["dataset"]: r.get("source") for r in c.registry()}
    assert reg.get("t"), "prebuilt _cube_sources was wiped -> get_cube rebuilt instead of opening"
    monkeypatch.setattr(cube_module, "_CUBE", None, raising=False)


def test_get_cube_builds_when_no_prebuilt_db(tmp_path, monkeypatch):
    from surimi_data_mcp.engine import cube as cube_module
    src = tmp_path / "src"; src.mkdir()
    (src / "x.csv").write_text("a,b\n1,10\n", encoding="utf-8")
    monkeypatch.setenv("SURIMI_CUBE_DB", str(tmp_path / "nope.duckdb"))  # absent
    monkeypatch.setenv("SURIMI_DATA_DIR", str(src))
    monkeypatch.setattr(cube_module, "_CUBE", None, raising=False)
    assert "x" in cube_module.get_cube().datasets()     # falls back to building
    monkeypatch.setattr(cube_module, "_CUBE", None, raising=False)
