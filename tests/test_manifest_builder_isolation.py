"""Experiment 2: one unscannable CSV must not cost the whole manifest.

The builder scans every CSV on one shared DuckDB connection. A scan that
aborts leaves the transaction aborted, and every file after it fails with
"Current transaction is aborted", so one bad file on a shared connection would leave
no manifest at all and explain_provenance with no lineage to report. Each file
scanned alone succeeds; only the sharing breaks it.
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)
_spec = importlib.util.spec_from_file_location(
    "build_sources_manifest", os.path.join(_HERE, "scripts", "build_sources_manifest.py"))
bsm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bsm)


def _csv(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_a_bad_file_does_not_poison_the_files_after_it(tmp_path):
    _csv(tmp_path, "a_good.csv", "year,v\n2020,1\n2021,2\n")
    # A file whose declared quoting cannot be parsed the way the cube reads it.
    (tmp_path / "b_bad.csv").write_bytes(b'year,v\n2020,"unterminated\n')
    _csv(tmp_path, "c_good.csv", "year,v\n1999,7\n")
    out = bsm.build(str(tmp_path))
    assert "a_good" in out and "c_good" in out, "a later good file must still be scanned"


def test_the_failure_is_recorded_not_swallowed(tmp_path, monkeypatch):
    """A file that cannot be scanned is recorded with an error rather than
    dropped. A silently missing dataset reads as "no such data", which is the
    false absence this project exists to prevent. _stats is forced to raise
    here because DuckDB parses almost anything: the contract under test is the
    recording, not DuckDB's tolerance."""
    _csv(tmp_path, "bad.csv", "year,v\n2020,1\n")
    _csv(tmp_path, "good.csv", "year,v\n2020,1\n")
    real = bsm._stats

    def boom(con, path):
        if path.name == "bad.csv":
            raise RuntimeError("Current transaction is aborted")
        return real(con, path)

    monkeypatch.setattr(bsm, "_stats", boom)
    out = bsm.build(str(tmp_path))
    assert "aborted" in out["bad"]["error"]
    assert out["bad"]["n_rows"] is None
    assert out["good"]["n_rows"] == 1, "a good file after a bad one still scans"


def test_a_clean_directory_still_reports_rows_and_years(tmp_path):
    _csv(tmp_path, "t.csv", "year,v\n2001,1\n2003,2\n")
    e = bsm.build(str(tmp_path))["t"]
    assert e["n_rows"] == 2 and e["first"] == 2001 and e["last"] == 2003
    assert e["missing"] == [2002]
