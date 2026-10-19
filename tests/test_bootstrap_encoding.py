"""Q-E6-1: bootstrap's Postgres loader loads a Latin-1 CSV.

`_default_pg_loader` calls `load_csv.load_table`, which detects the file's encoding (see
tests/test_load_csv_encoding.py). medits_cpue_all_species_wide.csv goes through this path
when the chart's load-data Job runs bootstrap().
"""
from __future__ import annotations

from pathlib import Path


class _SpyCursor:
    def __init__(self):
        self.calls: list[str] = []
        self.executemany_calls: list[tuple[str, list]] = []

    def execute(self, sql, params=None):
        self.calls.append(sql)

    def executemany(self, sql, batch):
        self.calls.append(sql)
        self.executemany_calls.append((sql, batch))

    def close(self):
        pass


class _SpyConnection:
    def __init__(self, cursor: _SpyCursor):
        self._cursor = cursor
        self.autocommit = False

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def test_default_pg_loader_handles_a_latin1_csv_without_raising(monkeypatch, tmp_path: Path):
    from surimi_data_mcp.loader import bootstrap
    from surimi_data_mcp.loader import load_csv as load_csv_mod
    import psycopg2

    p = tmp_path / "medits_sample.csv"
    p.write_bytes(
        "scient_name,common_name,y_1994\nAbra alba,\xe9tude,0.5\n".encode("latin-1")
    )
    # _default_pg_loader imports CSV_TO_TABLE from loader.load_csv at call
    # time (a local import inside the function), so the module-level name
    # actually consulted is loader.load_csv.CSV_TO_TABLE, not bootstrap's.
    monkeypatch.setattr(
        load_csv_mod, "CSV_TO_TABLE",
        {"medits_sample.csv": "surimi.medits_survey_cpue_mediterranean"},
    )

    cur = _SpyCursor()
    conn = _SpyConnection(cur)
    monkeypatch.setattr(psycopg2, "connect", lambda url: conn)

    loaded = bootstrap._default_pg_loader([p], "postgresql://fake")  # must not raise

    assert loaded == ["medits_sample.csv"]
    inserts = [
        batch for sql, batch in cur.executemany_calls
        if "medits_survey_cpue_mediterranean" in sql
    ]
    assert inserts
    row = inserts[0][0]
    assert row[0] == "Abra alba"
    assert row[1] == "\xe9tude"
