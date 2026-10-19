"""End-to-end: `loader/load_csv.py`'s main() inserts oecd.fsedata rows realigned.

Same smallest-spy-cursor convention as tests/test_load_csv_declared_types.py.
Pins the ACTUAL CREATE TABLE + inserted values, not just the intermediate
row-transform helper, so a future refactor that stops calling _realign_row
from main() fails here even if the helper itself still passes its own tests.
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


_HEADER = (
    "country.code,country.name,country.policy.code,country.policy.name,"
    "fse.category.code,fse.category.name,country.policy.description,"
    "support.mechanism,unit.of.measure.code,unit.of.measure.name,"
    "2000,2001,2002,2003,2004,2005,2006,2007,2008,2009,2010,2011,2012,"
    "2013,2014,2015,2016,2017,2018,2019,2020,2021,2022\n"
)

_CORRUPT_ROW = (
    ",,,,,,,,,,,7962777.30823013,4658344.6914562,4578726.24690849,"
    "6357012.343671,5123370.6578622,0,0,0,0,0,0,0,"
    "ARG,Argentina,IFINC_INS_ARG1,"
    '"Productive recovery programme (Argentina)",'
    "S07,Support to fisher income,"
    '"This program provides workers a fixed amount, up 12 months.",'
    "Transfer,USD,US dollar\n"
)


def test_main_writes_a_true_country_code_not_a_year_value(monkeypatch, tmp_path):
    from surimi_data_mcp.loader import load_csv as load_csv_mod
    import psycopg2

    fixture_map = {"hive_oecd_fsedata.csv": "oecd.fsedata"}
    monkeypatch.setattr(load_csv_mod, "CSV_TO_TABLE", fixture_map)

    (tmp_path / "hive_oecd_fsedata.csv").write_text(_HEADER + _CORRUPT_ROW)
    monkeypatch.setenv("CSV_DIR", str(tmp_path))

    cur = _SpyCursor()
    conn = _SpyConnection(cur)
    monkeypatch.setattr(psycopg2, "connect", lambda url: conn)

    load_csv_mod.main()

    inserts = [batch for sql, batch in cur.executemany_calls if "oecd.fsedata" in sql]
    assert inserts, "no INSERT issued for oecd.fsedata"
    row = inserts[0][0]

    headers = [h.strip() for h in _HEADER.strip().split(",")]
    idx = {h: i for i, h in enumerate(
        ["country_code", "country_name", "country_policy_code",
         "country_policy_name", "fse_category_code", "fse_category_name",
         "country_policy_description", "support_mechanism",
         "unit_of_measure_code", "unit_of_measure_name"]
        + [f"y_{y}" for y in range(2000, 2023)]
    )}

    # Pre-fix this cell held a numeric year value (or blank); post-fix it is
    # the real ISO-ish country code.
    assert row[idx["country_code"]] == "ARG"
    assert row[idx["country_name"]] == "Argentina"
    assert row[idx["support_mechanism"]] == "Transfer"

    # y_2011..y_2015 are genuinely numeric and must be cast to float, not left
    # as the string "ARG" the corrupted alignment would have put three slots
    # later (at y_2013).
    assert row[idx["y_2011"]] == 7962777.30823013
    assert isinstance(row[idx["y_2011"]], float)
    assert row[idx["y_2013"]] == 4578726.24690849
    assert row[idx["y_2016"]] == 0.0
    assert row[idx["y_2000"]] is None
