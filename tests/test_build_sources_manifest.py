# tests/test_build_sources_manifest.py
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from surimi_data_mcp.engine import cube

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_sources_manifest.py"


def test_manifest_hashes_and_counts_rows(tmp_path):
    csv = tmp_path / "hive_eu_dcf_demo_table.csv"
    csv.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    out = tmp_path / "sources_manifest.json"
    subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), str(out)], check=True)
    m = json.loads(out.read_text())
    entry = m["hive_eu_dcf_demo_table"]            # key == sanitized stem == dataset
    assert entry["file"] == "hive_eu_dcf_demo_table.csv"
    assert entry["sha256"] == hashlib.sha256(csv.read_bytes()).hexdigest()
    assert entry["n_rows"] == 2                    # data rows, header excluded
    assert "agency" in entry


def test_colliding_stems_get_cube_style_suffixes(tmp_path):
    # "odd headers.csv" and "odd.headers.csv" both sanitize to "odd_headers";
    # cube.build_cube names them odd_headers + odd_headers_2, manifest must too.
    (tmp_path / "odd headers.csv").write_text("a\n1\n", encoding="utf-8")
    (tmp_path / "odd.headers.csv").write_text("a\n1\n2\n", encoding="utf-8")
    out = tmp_path / "sources_manifest.json"
    subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), str(out)], check=True)
    m = json.loads(out.read_text())
    assert set(m) == {"odd_headers", "odd_headers_2"}
    # _discover_csvs sorts: "odd headers.csv" (space < dot) comes first
    assert m["odd_headers"]["file"] == "odd headers.csv"
    assert m["odd_headers"]["n_rows"] == 1
    assert m["odd_headers_2"]["file"] == "odd.headers.csv"
    assert m["odd_headers_2"]["n_rows"] == 2


def test_nested_csv_stores_lake_relative_path(tmp_path):
    # cube.build_cube records path.relative_to(base) as the source; the manifest
    # "file" must be the SAME lake-relative path (POSIX separators), or the
    # download URL built from it is wrong for any nested file.
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "x.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    out = tmp_path / "sources_manifest.json"
    subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), str(out)], check=True)
    m = json.loads(out.read_text())
    assert m["x"]["file"] == "subdir/x.csv"


def test_n_rows_counts_parsed_records_not_physical_lines(tmp_path):
    # A quoted field with an embedded newline is ONE record to DuckDB's
    # read_csv_auto (what the cube loads) but TWO physical lines. The manifest
    # must report the parsed record count the cube actually holds.
    csv = tmp_path / "quoted.csv"
    csv.write_text('a,b\n1,"first line\nsecond line"\n2,plain\n', encoding="utf-8")
    out = tmp_path / "sources_manifest.json"
    subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), str(out)], check=True)
    m = json.loads(out.read_text())
    assert m["quoted"]["n_rows"] == 2   # parsed records; physical lines would say 3


def test_manifest_reports_first_last_and_missing_years(tmp_path):
    # Q-E11-1: a filename/README can claim one span while the data holds
    # another. The manifest must report what the DATA says, not a copied span:
    # first/last from the real values, and which years in between are absent.
    csv = tmp_path / "hive_eu_dcf_demo_table.csv"
    csv.write_text("year,v\n2008,1\n2009,2\n2011,3\n", encoding="utf-8")  # 2010 absent
    out = tmp_path / "sources_manifest.json"
    subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), str(out)], check=True)
    entry = json.loads(out.read_text())["hive_eu_dcf_demo_table"]
    assert entry["first"] == 2008
    assert entry["last"] == 2011
    assert entry["missing"] == [2010]


def test_manifest_year_detection_is_case_insensitive(tmp_path):
    csv = tmp_path / "hive_eu_dcf_demo_table.csv"
    csv.write_text("Year,v\n2020,1\n2021,2\n", encoding="utf-8")
    out = tmp_path / "sources_manifest.json"
    subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), str(out)], check=True)
    entry = json.loads(out.read_text())["hive_eu_dcf_demo_table"]
    assert entry["first"] == 2020
    assert entry["last"] == 2021
    assert entry["missing"] == []


def test_manifest_temporal_fields_are_null_with_no_year_column(tmp_path):
    csv = tmp_path / "hive_eu_dcf_demo_table.csv"
    csv.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    out = tmp_path / "sources_manifest.json"
    subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), str(out)], check=True)
    entry = json.loads(out.read_text())["hive_eu_dcf_demo_table"]
    assert entry["first"] is None
    assert entry["last"] is None
    assert entry["missing"] == []


def test_manifest_keys_equal_cube_dataset_names(tmp_path):
    # Parity pin: the manifest's keys must equal the dataset names a REAL
    # cube.build_cube produces over the same dir (incl. nested files and a
    # stem collision), so receipts can always join manifest <-> cube.
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "nested.csv").write_text("a\n1\n", encoding="utf-8")
    (tmp_path / "odd headers.csv").write_text("a\n1\n", encoding="utf-8")
    (tmp_path / "odd.headers.csv").write_text("a\n1\n2\n", encoding="utf-8")
    out = tmp_path / "sources_manifest.json"
    subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), str(out)], check=True)
    m = json.loads(out.read_text())
    c = cube.build_cube(str(tmp_path))
    try:
        assert set(m.keys()) == set(c.datasets())
    finally:
        c.close()
