"""Live columns keep the wide-year steering (2026-10-13).

list_columns reads columns from the database and, when that worked, returned them bare. The note
that a run of y_YYYY columns is a time series (and the time column) came only from the static
catalogue, so on EDITO, where the database answers, list_columns('oecd-fsedata') returned 23 year
columns with nothing saying they form a series: the dead end the note was written to prevent.
"""
from surimi_data_mcp.tools import discovery


class _LiveDatabase:
    def describe_table(self, table):
        cols = [{"name": "country_code", "type": "varchar"}]
        cols += [{"name": f"y_{y}", "type": "double"} for y in range(2000, 2023)]
        return {"columns": cols, "error": None}


def test_live_columns_still_say_they_are_a_year_series(monkeypatch):
    monkeypatch.setattr(discovery, "get_trino", lambda: _LiveDatabase())
    out = discovery.list_columns("oecd-fsedata")
    assert [c["name"] for c in out["columns"]][:2] == ["country_code", "y_2000"]
    assert out["wide_years"]["count"] == 23
    assert out["wide_years"]["first"] == 2000 and out["wide_years"]["last"] == 2022
    assert "WIDE time series" in out["next_step"]
    assert "time_column" in out


def test_live_columns_without_a_year_run_carry_no_year_note(monkeypatch):
    class _Plain:
        def describe_table(self, table):
            return {"columns": [{"name": "year", "type": "int"}, {"name": "value", "type": "double"}],
                    "error": None}
    monkeypatch.setattr(discovery, "get_trino", lambda: _Plain())
    out = discovery.list_columns("aer-capacity")
    assert [c["name"] for c in out["columns"]] == ["year", "value"]
    assert "wide_years" not in out and "next_step" not in out
