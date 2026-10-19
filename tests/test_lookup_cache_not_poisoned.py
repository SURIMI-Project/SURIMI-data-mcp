"""A lookup that failed because the backend was not ready must not cache that.

Seen in production 2026-09-18 on release surimi-data-mcp-928858. The load job
takes ~48 minutes; anything that queries a Trino-backed table before it finishes
gets an exception. Both vocabulary loaders caught it, cached an EMPTY mapping,
and never retried, so:

    query_data   SELECT ... WHERE alpha3_code='COD'   -> Atlantic cod, Gadus morhua
    explain_species_code COD                          -> found: false

The table was there; the cache said otherwise, and would keep saying so until
the pod restarted. A single early call disables the feature for the life of the
process.

The rule: cache a SUCCESSFUL read, never a failure. An empty result from a
working backend is indistinguishable here from a failed read, so neither is
cached and the next call tries again. The cost of retrying is one query on a
path that is already degraded.
"""
from __future__ import annotations

import pytest

from surimi_data_mcp.knowledge import lookups


@pytest.fixture(autouse=True)
def _clear_caches():
    lookups._SPECIES_TABLE_CACHE = None
    lookups._EAV_CACHE = None
    yield
    lookups._SPECIES_TABLE_CACHE = None
    lookups._EAV_CACHE = None


def test_a_failed_species_lookup_is_retried_not_remembered(monkeypatch):
    """The production bug: backend down at first call, up at the second."""
    calls = {"n": 0}

    def flaky(sql, limit=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Trino not ready: table does not exist yet")
        return {"rows": [{"alpha3_code": "COD", "common_name": "Atlantic cod",
                          "scient_name": "Gadus morhua"}]}

    monkeypatch.setattr(lookups, "_species_table_client", lambda: flaky)

    assert lookups._species_table_lookup("COD") is None          # backend down
    hit = lookups._species_table_lookup("COD")                   # backend up
    assert hit is not None, "a failed read must not be cached as 'no such code'"
    assert hit["scientific_name"] == "Gadus morhua"
    assert calls["n"] == 2, "the second call must actually re-query"


def test_a_successful_species_read_is_cached(monkeypatch):
    """Caching still works; 1,306 rows are not re-read per lookup."""
    calls = {"n": 0}

    def ok(sql, limit=None):
        calls["n"] += 1
        return {"rows": [{"alpha3_code": "COD", "common_name": "Atlantic cod",
                          "scient_name": "Gadus morhua"}]}

    monkeypatch.setattr(lookups, "_species_table_client", lambda: ok)
    lookups._species_table_lookup("COD")
    lookups._species_table_lookup("COD")
    lookups._species_table_lookup("MAC")
    assert calls["n"] == 1, "a good read must be cached"


def test_a_failed_eav_load_is_retried_not_remembered(monkeypatch):
    """Same defect, older code path: the EAV vocabulary reads the cube."""
    calls = {"n": 0}

    def flaky_vocab():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("cube not built yet")
        return {"totdirsub": {"definition": "Operating subsidies (Income)",
                              "unit": "euro", "source": "EU DCF",
                              "dataset_id": "aer-economic-national-level"}}

    monkeypatch.setattr(lookups, "_read_eav_vocabulary", flaky_vocab)

    assert lookups._eav_vocabulary() == {}                # backend down
    assert "totdirsub" in lookups._eav_vocabulary()       # backend up, retried
    assert calls["n"] == 2
