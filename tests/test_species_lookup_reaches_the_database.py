"""A species code missing from the vocabulary is looked up in the landings table (2026-10-13).

The restructure moved the database client from server.py to tools/common.py, and the lookup kept
importing it from server. The AttributeError was caught as a transient failure, so every code the
vocabulary did not know answered found:false on 1.2.48. The tests that existed replaced
_species_table_client itself and never ran it. This one runs it, with only the database faked.
"""
from __future__ import annotations

from surimi_data_mcp.knowledge import lookups
from surimi_data_mcp.tools import common


class _FakeDatabase:
    def __init__(self):
        self.queries = []

    def execute(self, sql, limit=500):
        self.queries.append(sql)
        return {"rows": [{"alpha3_code": "ZZQ", "common_name": "Test fish", "scient_name": "Pisces testus"}]}


def test_the_lookup_reads_the_landings_table_through_the_shared_client(monkeypatch):
    db = _FakeDatabase()
    monkeypatch.setattr(common, "get_trino", lambda: db)
    monkeypatch.setattr(lookups, "_SPECIES_TABLE_CACHE", None)
    hit = lookups._species_table_lookup("ZZQ")
    assert db.queries and "fisheries_landings" in db.queries[0]
    assert hit is not None and hit["scientific_name"] == "Pisces testus"


def test_a_code_missing_from_the_vocabulary_is_found_in_the_table(monkeypatch):
    db = _FakeDatabase()
    monkeypatch.setattr(common, "get_trino", lambda: db)
    monkeypatch.setattr(lookups, "_SPECIES_TABLE_CACHE", None)
    out = lookups.explain_species_code("ZZQ")
    assert out["found"] is True
    assert out["scientific_name"] == "Pisces testus"
