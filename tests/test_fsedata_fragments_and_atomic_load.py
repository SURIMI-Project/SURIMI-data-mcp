"""Two ingest defects found on 2026-09-25.

1. oecd-fsedata carries 1,462 FRAGMENT rows among its 2,784 records: the
   upstream export wrote each multi-line policy description as extra CSV
   lines, split at commas and padded to 33 fields. The rotation repair
   treated them as records, so the served cube held 20 bogus country codes
   (' 19 April 2019', a URL, '356', ...) and 62 fragments put number-like
   text into year columns, which an unfiltered SUM(y_YYYY) then added in.
   A record's realigned country_code is a three-letter code; a fragment's
   is not.

2. The Postgres loaders dropped each live table, recreated it and inserted
   in autocommitted 10,000-row batches, so for the 72 minutes of a load
   query_data could return a signed total over a half-loaded table. Rows now
   go into <table>__loading, and the old table is replaced in one
   transaction only once the new one is complete.
"""
from __future__ import annotations

import csv as _csv
from pathlib import Path

import pytest

_HEADER = [
    "country.code", "country.name", "country.policy.code", "country.policy.name",
    "fse.category.code", "fse.category.name", "country.policy.description",
    "support.mechanism", "unit.of.measure.code", "unit.of.measure.name",
] + [str(y) for y in range(2000, 2023)]

_RECORD = (
    [""] * 11 + ["7962777.30823013", "4658344.6914562", "4578726.24690849",
                 "6357012.343671", "5123370.6578622"] + ["0"] * 7
    + ["ARG", "Argentina", "IFINC_INS_ARG1", "Productive recovery programme",
       "S07", "Support to fisher income", "", "Transfer", "USD", "US dollar"]
)
# Continuation lines of the description above, as the export wrote them.
_FRAGMENTS = [
    ["a) Annual tariffs with fishing licences"] + [""] * 32,
    ["b) Catching fees", " authorized for commercial fishing", "", "356"] + [""] * 29,
    [" 19 April 2019", " http://example.gov/notice"] + [""] * 31,
]


def _write(path: Path, rows):
    with open(path, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(_HEADER)
        for r in rows:
            w.writerow(r)


# ── cube ───────────────────────────────────────────────────────────────────

def test_the_cube_drops_fragment_rows(tmp_path):
    from surimi_data_mcp.engine.cube import build_cube
    root = tmp_path / "lake"
    root.mkdir()
    _write(root / "hive_oecd_fsedata.csv", [_RECORD] + _FRAGMENTS)
    c = build_cube(str(root))
    rows = c.query("SELECT country_code FROM hive_oecd_fsedata")
    assert [r["country_code"] for r in rows] == ["ARG"]


def test_a_fragments_number_is_not_summed_into_a_year(tmp_path):
    from surimi_data_mcp.engine.cube import build_cube
    root = tmp_path / "lake"
    root.mkdir()
    _write(root / "hive_oecd_fsedata.csv", [_RECORD] + _FRAGMENTS)
    c = build_cube(str(root))
    # raw field 3 of a fragment lands in y_2003 after the rotation
    got = c.query("SELECT SUM(TRY_CAST(y_2003 AS DOUBLE)) AS s FROM hive_oecd_fsedata")[0]["s"]
    assert got is None or got == pytest.approx(0.0)


# ── Postgres loaders ───────────────────────────────────────────────────────

class _SpyCursor:
    def __init__(self):
        self.calls: list[str] = []
        self.rows: dict[str, list] = {}

    def execute(self, sql, params=None):
        self.calls.append(" ".join(sql.split()))

    def executemany(self, sql, batch):
        self.calls.append(" ".join(sql.split()))
        table = sql.split("INSERT INTO ")[1].split(" ")[0]
        self.rows.setdefault(table, []).extend(batch)

    def close(self):
        pass


class _SpyConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.autocommit = False

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def _run_load_csv_main(monkeypatch, tmp_path):
    from surimi_data_mcp.loader import load_csv as lc
    import psycopg2
    monkeypatch.setattr(lc, "CSV_TO_TABLE", {"hive_oecd_fsedata.csv": "oecd.fsedata"})
    _write(tmp_path / "hive_oecd_fsedata.csv", [_RECORD] + _FRAGMENTS)
    monkeypatch.setenv("CSV_DIR", str(tmp_path))
    cur = _SpyCursor()
    monkeypatch.setattr(psycopg2, "connect", lambda url: _SpyConnection(cur))
    lc.main()
    return cur


def _run_bootstrap_loader(monkeypatch, tmp_path):
    from surimi_data_mcp.loader import bootstrap
    import psycopg2
    from surimi_data_mcp.loader import load_csv as lc
    monkeypatch.setattr(lc, "CSV_TO_TABLE", {"hive_oecd_fsedata.csv": "oecd.fsedata"})
    path = tmp_path / "hive_oecd_fsedata.csv"
    _write(path, [_RECORD] + _FRAGMENTS)
    cur = _SpyCursor()
    monkeypatch.setattr(psycopg2, "connect", lambda url: _SpyConnection(cur))
    bootstrap._default_pg_loader([path], "postgresql://spy")
    return cur


@pytest.mark.parametrize("run", [_run_load_csv_main, _run_bootstrap_loader])
def test_fragment_rows_are_not_loaded_into_postgres(run, monkeypatch, tmp_path):
    cur = run(monkeypatch, tmp_path)
    rows = cur.rows["oecd.fsedata__loading"]
    assert len(rows) == 1
    assert rows[0][0] == "ARG"


@pytest.mark.parametrize("run", [_run_load_csv_main, _run_bootstrap_loader])
def test_the_live_table_is_replaced_only_after_the_new_one_is_complete(run, monkeypatch, tmp_path):
    cur = run(monkeypatch, tmp_path)
    calls = cur.calls
    last_insert = max(i for i, c in enumerate(calls) if c.startswith("INSERT INTO oecd.fsedata__loading"))
    drop_live = [i for i, c in enumerate(calls) if c == "DROP TABLE IF EXISTS oecd.fsedata"]
    assert drop_live and all(i > last_insert for i in drop_live), calls
    i = drop_live[0]
    assert calls[i - 1] == "BEGIN"
    assert calls[i + 1] == "ALTER TABLE oecd.fsedata__loading RENAME TO fsedata"
    assert calls[i + 2] == "COMMIT"
    assert not any(c.startswith("INSERT INTO oecd.fsedata ") for c in calls)
