"""Characterization + fix tests: does `query_data` reach the semantic views?

The views (`datasets/semantic_views.py`) live ONLY as DuckDB views baked into
the cube -- they are not real Trino/Postgres tables, so the primary backend
(Postgres on EDITO) always reports them table-not-found. `query_data`'s
`CubeFallbackClient` already reroutes a Postgres table-miss to the cube for
FQN-style references (`hive.<schema>.<table>`), but the views are registered
in the catalog under a BARE flat name (`v_sales_by_species_year`, no `hive.`
prefix) -- see `datasets/datasets.yaml`. A model-written
``SELECT ... FROM v_sales_by_species_year`` therefore carries no FQN for
`_rewrite_fqns_to_cube` to match, so before the fix in this file the fallback
found "nothing referenced" and the query dead-ended on the primary's false
"can't retrieve", even though the cube has the view.

Test 1 pins that the view is queryable directly on a real cube connection
(sanity: the view itself is fine). Test 2 exercises the REAL `query_data`
wiring end-to-end (the same `CubeFallbackClient(primary, cube, sanitize)`
construction `tools.common.query_client` uses) with a fake primary that
returns the exact "relation ... does not exist" error shape Postgres raises,
so the fallback path is exercised without a live Postgres. Tests 3-5 pin the
new bare-name branch at the `CubeFallbackClient` unit level with a fake cube,
and confirm the existing dotted-FQN fallback + fail-closed behaviour still
hold after the extension.
"""
from __future__ import annotations

import os
from typing import Any

import pytest

from surimi_data_mcp.paths import APP_ROOT
from surimi_data_mcp.engine.cube import _sanitize_name, build_cube
from surimi_data_mcp.engine.sql_tools import CubeFallbackClient, _bare_relation_references_cube, make_query_data_signed

# The real CSV exports sit in data/exports/ where the data is loaded (not in a fresh clone).
EXPORTS = str(APP_ROOT / "data" / "exports")


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")


class _PostgresMissPrimary:
    """Stub mimicking `PostgresClient.execute()` when Postgres lacks a table.

    Real Postgres (via psycopg2) raises `relation "<table>" does not exist`
    on a missing relation; `PostgresClient.execute()` catches it and returns
    `{"error": str(e), ...}` (see `engine/databases.py`). This stub reproduces
    that exact shape so `CubeFallbackClient`'s `_TABLE_MISS` regex is
    exercised honestly, without needing a live Postgres connection.
    """

    def __init__(self, table_name: str):
        self._table_name = table_name
        self.calls: list[tuple[str, int]] = []

    def execute(self, sql: str, limit: int = 500) -> dict[str, Any]:
        self.calls.append((sql, limit))
        return {
            "columns": [], "rows": [], "row_count": 0, "truncated": False,
            "error": (
                f'relation "{self._table_name}" does not exist\n'
                f"LINE 1: SELECT * FROM {self._table_name}\n"
                f"                      ^\n"
            ),
        }

    def describe_table(self, table: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError


class _FakeCube:
    """Minimal fake matching the `Cube` interface `CubeFallbackClient` needs."""

    def __init__(self, names, rows):
        self._names = list(names)
        self._rows = rows
        self.queries: list[str] = []

    def datasets(self):
        return list(self._names)

    def query(self, sql: str):
        self.queries.append(sql)
        return list(self._rows)


# ── Test 1: sanity -- the view itself is queryable on a real cube connection ─
@pytest.mark.skipif(not os.path.isdir(EXPORTS), reason="real exports not present")
def test_view_queryable_directly_on_real_cube():
    c = build_cube(EXPORTS)
    rows = c.con.execute(
        "SELECT species_code, value_meur FROM v_sales_by_species_year "
        "WHERE year='2030' ORDER BY value_meur DESC"
    ).fetchall()
    assert rows[0] == ("MAC", 59.62)


# ── Test 2: the REAL query_data wiring, end-to-end ───────────────────────────
@pytest.mark.skipif(not os.path.isdir(EXPORTS), reason="real exports not present")
def test_query_data_reaches_bare_semantic_view_via_cube_fallback():
    """Characterizes the actual bug: query_data(...) on a bare view name.

    Wired exactly as `tools.common.query_client()` builds it:
    `CubeFallbackClient(primary, cube, cube_module._sanitize_name)` behind
    `make_query_data_signed`. Before the fix, this FAILS (the primary's
    "does not exist" error passes through unchanged, because the bare name
    has no FQN for `_rewrite_fqns_to_cube` to match). After the fix, it must
    return MAC 59.62 served by the cube.
    """
    cube = build_cube(EXPORTS)
    primary = _PostgresMissPrimary("v_sales_by_species_year")
    fn = make_query_data_signed(
        client_factory=lambda: CubeFallbackClient(primary, cube, _sanitize_name),
        backend_id="trino://hive@127.0.0.1:8081",
    )
    out = fn(
        sql="SELECT species_code, value_meur FROM v_sales_by_species_year "
            "WHERE year='2030' ORDER BY value_meur DESC",
        limit=10,
    )
    assert out["value"]["error"] is None, out["value"]["error"]
    rows = out["value"]["rows"]
    assert rows[0]["species_code"] == "MAC"
    assert rows[0]["value_meur"] == pytest.approx(59.62)
    assert out["receipt"]["provenance"]["backend"] == "duckdb-cube"
    assert primary.calls, "primary must have been tried first (table-miss), not skipped"


# ── Tests 3-5: CubeFallbackClient unit level (fake cube, no real exports) ────
def test_cube_fallback_serves_bare_view_name_on_table_miss():
    cube = _FakeCube(
        names=["v_sales_by_species_year"],
        rows=[{"species_code": "MAC", "value_meur": 59.62}],
    )
    primary = _PostgresMissPrimary("v_sales_by_species_year")
    c = CubeFallbackClient(primary, cube)
    res = c.execute(
        "SELECT species_code, value_meur FROM v_sales_by_species_year "
        "WHERE year='2030'",
        limit=10,
    )
    assert res["error"] is None
    assert res["served_by"] == "duckdb-cube"
    assert res["rows"] == [{"species_code": "MAC", "value_meur": 59.62}]
    assert cube.queries and "v_sales_by_species_year" in cube.queries[0]


def test_cube_fallback_still_fails_closed_for_genuinely_missing_bare_table():
    """A bare name that is NOT a real cube relation must still fail closed --
    the extension must not silently serve (empty or otherwise) results for a
    table that genuinely does not exist anywhere."""
    cube = _FakeCube(names=["v_sales_by_species_year"], rows=[{"a": 1}])
    primary = _PostgresMissPrimary("totally_missing_table")
    c = CubeFallbackClient(primary, cube)
    res = c.execute("SELECT * FROM totally_missing_table", limit=5)
    assert "served_by" not in res
    assert "does not exist" in res["error"]
    assert cube.queries == []


def test_dotted_fqn_fallback_still_works_after_bare_name_extension():
    """Regression guard: the bare-name branch must not disturb the existing
    dotted-FQN (`hive.<schema>.<table>`) fallback path."""
    biomass_fqn = "hive.surimi.surimi_empirical_biomass_surveys_western_mediterranean"
    biomass_cube = "hive_surimi_surimi_empirical_biomass_surveys_western_mediterranean"
    cube = _FakeCube(names=[biomass_cube], rows=[{"year": 2020, "biomass_kt": 12.5}])
    primary = _PostgresMissPrimary(
        "surimi.surimi_empirical_biomass_surveys_western_mediterranean"
    )
    c = CubeFallbackClient(primary, cube)
    res = c.execute(f"SELECT * FROM {biomass_fqn} LIMIT 3", limit=3)
    assert res["error"] is None
    assert res["served_by"] == "duckdb-cube"
    assert biomass_cube in res["cube_sql"]


# ── Test 6: _BARE_REL must not truncate a dotted FQN's schema segment ───────
def test_bare_relation_regex_ignores_dotted_fqn_even_with_short_prefix_name():
    """A dotted FQN (``hive.<schema>.<table>``) must never yield a truncated
    bare match on its first segment, even if the cube happens to have a
    dataset literally named after that (short) prefix. Before the
    ``(?![\\w.])`` fix, the trailing ``(?!\\s*\\.)`` lookahead let ``\\w*``
    backtrack past the dot, capturing a truncated ``hiv`` out of ``hive`` --
    which, if ``"hiv"`` were ever a real cube dataset name, would falsely
    match here. This only resolves via the dotted-FQN path
    (`_rewrite_fqns_to_cube`), never via the bare-name branch.
    """
    cube_names = ["hiv"]
    assert _bare_relation_references_cube(
        "SELECT * FROM hive.eu_dcf.aer_capacity", cube_names, _sanitize_name
    ) is False

    # A genuine bare (undotted) view name must still match.
    cube_names_with_view = ["v_sales_by_species_year"]
    assert _bare_relation_references_cube(
        "SELECT * FROM v_sales_by_species_year", cube_names_with_view, _sanitize_name
    ) is True
