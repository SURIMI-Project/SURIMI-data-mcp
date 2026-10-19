"""Q-E6-1: medits_cpue_all_species_wide.csv is Latin-1, not UTF-8.

The ``surimi-biomass-medits`` entry in ``datasets.yaml`` points at it. MEDITS carries a
byte (0xE9, "e" with an accent in the source lab's export) that is not valid UTF-8 on its
own, so a read with the default encoding raises ``UnicodeDecodeError`` before a single row
loads. ``_detect_encoding`` in ``loader/load_csv.py`` tries an incremental UTF-8 decode of
the file and falls back to Latin-1, which never fails to decode. ``_infer_table_types`` and
``load_table`` open the file with that encoding. The tests use a minimal fixture and not the
real file.
"""
from __future__ import annotations


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


def test_detect_encoding_returns_utf8_for_clean_ascii(tmp_path):
    from surimi_data_mcp.loader.load_csv import _detect_encoding

    p = tmp_path / "clean.csv"
    p.write_text("scient_name,year\nGadus morhua,2020\n", encoding="utf-8")
    assert _detect_encoding(str(p)) == "utf-8"


def test_detect_encoding_falls_back_to_latin1_on_invalid_utf8_byte(tmp_path):
    from surimi_data_mcp.loader.load_csv import _detect_encoding

    p = tmp_path / "medits_sample.csv"
    p.write_bytes("scient_name,common_name\nAbra alba,\xe9tude\n".encode("latin-1"))
    assert _detect_encoding(str(p)) == "latin-1"


def test_infer_table_types_does_not_raise_on_latin1_file(tmp_path):
    from surimi_data_mcp.loader.load_csv import _infer_table_types

    p = tmp_path / "medits_sample.csv"
    p.write_bytes(
        "scient_name,common_name,y_1994\nAbra alba,\xe9tude,0.5\n".encode("latin-1")
    )
    headers, types = _infer_table_types(str(p))
    assert headers == ["scient_name", "common_name", "y_1994"]
    assert types == ["TEXT", "TEXT", "DOUBLE PRECISION"]


def test_main_loads_a_latin1_csv_end_to_end_without_raising(monkeypatch, tmp_path):
    from surimi_data_mcp.loader import load_csv as load_csv_mod
    import psycopg2

    fixture_map = {"medits_sample.csv": "surimi.medits_survey_cpue_mediterranean"}
    monkeypatch.setattr(load_csv_mod, "CSV_TO_TABLE", fixture_map)

    (tmp_path / "medits_sample.csv").write_bytes(
        "scient_name,common_name,y_1994\nAbra alba,\xe9tude,0.5\n".encode("latin-1")
    )
    monkeypatch.setenv("CSV_DIR", str(tmp_path))

    cur = _SpyCursor()
    conn = _SpyConnection(cur)
    monkeypatch.setattr(psycopg2, "connect", lambda url: conn)

    load_csv_mod.main()  # must not raise UnicodeDecodeError

    inserts = [
        batch for sql, batch in cur.executemany_calls
        if "medits_survey_cpue_mediterranean" in sql
    ]
    assert inserts
    row = inserts[0][0]  # scient_name, common_name, y_1994
    assert row[0] == "Abra alba"
    assert row[1] == "\xe9tude"  # decoded correctly, not mangled or dropped
    assert row[2] == 0.5
