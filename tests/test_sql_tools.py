"""Tests for the signed SQL tool wrapper that pairs with the existing
query_data implementation in engine/databases.py.

Uses a fake TrinoClient so we do not need a running Trino on localhost.
The wrapper itself is what we test: did the receipt sign the actual
SQL + limit + result?
"""
from __future__ import annotations

from typing import Any

import pytest

from surimi_data_mcp.provenance.receipts import verify_receipt


SECRET = b"sql-test-secret"


class _FakeClient:
    def __init__(self, rows: list[dict[str, Any]] | None = None):
        self.rows = rows or [{"country": "ESP", "n": 1234}]
        self.calls: list[tuple[str, int]] = []

    def execute(self, sql: str, limit: int = 500) -> dict[str, Any]:
        self.calls.append((sql, limit))
        return {
            "rows": self.rows,
            "columns": ["country", "n"],
            "row_count": len(self.rows),
            "error": None,
        }


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", SECRET.decode())


def test_query_data_signed_returns_value_and_receipt():
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed

    fake = _FakeClient()
    fn = make_query_data_signed(client_factory=lambda: fake)
    out = fn(sql="SELECT 1", limit=10)
    assert "value" in out
    assert "receipt" in out
    assert out["value"]["row_count"] == 1


def test_query_data_signed_records_sql_and_limit_in_receipt():
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed

    fn = make_query_data_signed(client_factory=_FakeClient)
    out = fn(sql="SELECT country FROM x", limit=42)
    params = out["receipt"]["input_params"]
    assert params["sql"] == "SELECT country FROM x"
    assert params["limit"] == 42


def test_query_data_signed_receipt_verifies():
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed

    fn = make_query_data_signed(client_factory=_FakeClient)
    out = fn(sql="SELECT 1", limit=1)
    v = verify_receipt(out["receipt"], secret=SECRET)
    assert v["verified"] is True


def test_query_data_signed_receipt_tool_id():
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed

    fn = make_query_data_signed(client_factory=_FakeClient)
    out = fn(sql="SELECT 1", limit=1)
    assert out["receipt"]["tool_id"] == "query_data"


def test_query_data_signed_rejects_tampered_rows():
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed

    fn = make_query_data_signed(client_factory=_FakeClient)
    out = fn(sql="SELECT 1", limit=1)
    out["receipt"]["output_value"]["rows"][0]["n"] = 999999  # tamper
    v = verify_receipt(out["receipt"], secret=SECRET)
    assert v["verified"] is False
    assert v["reason"] == "signature_mismatch"


def test_query_data_signed_propagates_executor_errors():
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed

    class _Broken(_FakeClient):
        def execute(self, sql: str, limit: int = 500) -> dict[str, Any]:
            raise RuntimeError("trino down")

    fn = make_query_data_signed(client_factory=_Broken)
    with pytest.raises(RuntimeError, match="trino down"):
        fn(sql="SELECT 1", limit=1)


def test_query_data_signed_returns_query_provenance():
    """Provenance should include the trino backend so the verifier can
    distinguish results from different sources."""
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed

    fn = make_query_data_signed(
        client_factory=_FakeClient,
        backend_id="trino://hive@127.0.0.1:8081",
    )
    out = fn(sql="SELECT 1", limit=1)
    assert out["receipt"]["provenance"]["backend"] == "trino://hive@127.0.0.1:8081"


# ── is_aggregate_sql detector tests ──────────────────────────────────────────

def test_detects_group_by():
    from surimi_data_mcp.engine.sql_tools import is_aggregate_sql
    assert is_aggregate_sql("SELECT country, SUM(x) FROM t GROUP BY country")


def test_detects_bare_aggregate():
    from surimi_data_mcp.engine.sql_tools import is_aggregate_sql
    assert is_aggregate_sql("select sum(number_of_vessels) from t")


def test_detects_count_star():
    from surimi_data_mcp.engine.sql_tools import is_aggregate_sql
    assert is_aggregate_sql("SELECT COUNT(*) FROM t")


def test_row_select_passes():
    from surimi_data_mcp.engine.sql_tools import is_aggregate_sql
    assert not is_aggregate_sql("SELECT year, country, number_of_vessels FROM t WHERE year=2024 LIMIT 20")


def test_does_not_falsely_flag_summary_word():
    # 'summary' contains 'sum' but is not an aggregate call
    from surimi_data_mcp.engine.sql_tools import is_aggregate_sql
    assert not is_aggregate_sql("SELECT summary FROM reports LIMIT 5")


def test_flags_cross_year_country_sum():
    from surimi_data_mcp.engine.sql_tools import is_aggregate_sql
    assert is_aggregate_sql("SELECT country_name, SUM(number_of_vessels) FROM hive.eu_dcf.aer_capacity GROUP BY country_name ORDER BY 2 DESC")


def test_passes_row_inspection_with_limit():
    from surimi_data_mcp.engine.sql_tools import is_aggregate_sql
    assert not is_aggregate_sql("SELECT year, country_name, number_of_vessels FROM hive.eu_dcf.aer_capacity WHERE country_name='Italy' ORDER BY year LIMIT 20")


# ── eav_magnitude_refusal: block the confident-wrong EAV double-count ─────────
# The AER social/economic EAV tables store many OVERLAPPING strata (by gender,
# age, geo_indicator, fishing_tech ...) all under the same variable_code, so a
# raw SUM(variable_value) MULTI-COUNTS every person (socemploy 2020: naive
# 442,638 vs the ~104k gender-attributed slice). Even pinning one variable_code
# + year does not fix it. Hand-summing this column via query_data is essentially
# never safe; the aggregate/breakdown tools (group by ONE dimension, exclude
# NULL keys) are the correct path. So we REFUSE a magnitude aggregate over
# variable_value rather than run it and sign a wrong number ("say I can't total
# this" beats a confident wrong total). COUNT is a row tally, still allowed.

def test_eav_refusal_flags_sum_variable_value():
    from surimi_data_mcp.engine.sql_tools import eav_magnitude_refusal
    r = eav_magnitude_refusal(
        "SELECT SUM(variable_value) FROM hive.eu_dcf.aer_social_data_2017_2020 "
        "WHERE variable_code='socemploy' AND year=2020")
    assert r  # a non-empty refusal reason
    assert "aggregate" in r.lower() or "breakdown" in r.lower()  # points to the fix


def test_eav_refusal_flags_avg_and_case_insensitive_and_quoted():
    from surimi_data_mcp.engine.sql_tools import eav_magnitude_refusal
    assert eav_magnitude_refusal("select avg(variable_value) from t")
    assert eav_magnitude_refusal('SELECT SUM("variable_value") FROM t')
    assert eav_magnitude_refusal("SELECT MIN(variable_value), MAX(variable_value) FROM t")


def test_eav_refusal_ignores_normal_column_aggregate():
    from surimi_data_mcp.engine.sql_tools import eav_magnitude_refusal
    # a magnitude on a NORMAL column is fine (still gets the steering note, but
    # is NOT refused) -- narrow guard, no blanket-refusal regression.
    assert eav_magnitude_refusal(
        "SELECT country_name, SUM(number_of_vessels) FROM hive.eu_dcf.aer_capacity "
        "GROUP BY country_name") is None


def test_eav_refusal_allows_count_variable_value():
    from surimi_data_mcp.engine.sql_tools import eav_magnitude_refusal
    # COUNT is a row tally (how many records), not a magnitude double-count.
    assert eav_magnitude_refusal("SELECT COUNT(variable_value) FROM t") is None
    assert eav_magnitude_refusal("SELECT COUNT(*) FROM t WHERE variable_code='socemploy'") is None


def test_eav_refusal_allows_row_lookup_of_variable_value():
    from surimi_data_mcp.engine.sql_tools import eav_magnitude_refusal
    # selecting the column (no aggregate) is a row lookup -- allowed.
    assert eav_magnitude_refusal(
        "SELECT gender, variable_value FROM hive.eu_dcf.aer_social_data_2017_2020 "
        "WHERE variable_code='socemploy' LIMIT 20") is None


# ── bypass forms (0.8.3): the model wraps variable_value in CAST/CASE/TRY_CAST ──
# The 0.8.2 guard matched only the literal SUM(variable_value); the live model
# wrote SUM(CAST(variable_value AS DOUBLE)) and SUM(CASE WHEN variable_code=...
# THEN variable_value END), which slipped through -> the 442,640 double-count
# survived in production (6 cases). The rule must be wrapper-agnostic: a
# MAGNITUDE aggregate co-occurring with a variable_value token is refused,
# however the column is wrapped.

def test_eav_refusal_catches_cast_wrapper():
    from surimi_data_mcp.engine.sql_tools import eav_magnitude_refusal
    assert eav_magnitude_refusal(
        "SELECT SUM(CAST(variable_value AS DOUBLE)) FROM "
        "hive.eu_dcf.aer_social_data_2017_2020 WHERE variable_code='socemploy'")


def test_eav_refusal_catches_try_cast_wrapper():
    from surimi_data_mcp.engine.sql_tools import eav_magnitude_refusal
    assert eav_magnitude_refusal(
        "SELECT AVG(TRY_CAST(variable_value AS DECIMAL(18,2))) FROM t")


def test_eav_refusal_catches_case_expression():
    # the exact form seen bypassing live (conservation-ngo-03 T8)
    from surimi_data_mcp.engine.sql_tools import eav_magnitude_refusal
    assert eav_magnitude_refusal(
        "SELECT gender, SUM(CASE WHEN variable_code='socemploy' THEN "
        "variable_value WHEN variable_code='socfte' THEN variable_value END) "
        "FROM hive.eu_dcf.aer_social_data_2017_2020 GROUP BY gender")


def test_eav_refusal_catches_grouped_cast_sum():
    # the grouped form that computes the double-count total per group
    from surimi_data_mcp.engine.sql_tools import eav_magnitude_refusal
    assert eav_magnitude_refusal(
        "SELECT year, SUM(CAST(variable_value AS DOUBLE)) FROM "
        "hive.eu_dcf.aer_social_data_2017_2020 WHERE variable_code='socemploy' "
        "GROUP BY year")


def test_eav_refusal_still_ignores_normal_aggregate_with_no_variable_value():
    # broadening must NOT catch a normal-column aggregate that never mentions
    # variable_value (even with a variable_code filter present).
    from surimi_data_mcp.engine.sql_tools import eav_magnitude_refusal
    assert eav_magnitude_refusal(
        "SELECT country_name, SUM(number_of_vessels) FROM hive.eu_dcf.aer_capacity "
        "WHERE year=2024 GROUP BY country_name") is None
    # a COUNT over an EAV table that filters (not sums) variable_value stays a
    # row tally -> allowed (COUNT is not a magnitude aggregate).
    assert eav_magnitude_refusal(
        "SELECT COUNT(*) FROM hive.eu_dcf.aer_social_data_2017_2020 "
        "WHERE variable_code='socemploy'") is None


# ── landings_double_count_refusal: block the area/division double-count ──────
# fisheries_landings (area-level) and fisheries_landings_divisions
# (division-level) are two GRANULARITIES of the same catch, not two disjoint
# tables -- every division row aggregates into its parent area row, so a raw
# SUM/AVG across both double-counts (2008 area 27: naive 16,781,564.29 vs the
# correct single-table 8,576,976.49). Same shape as eav_magnitude_refusal:
# refuse on a magnitude aggregate co-occurring with BOTH table-name tokens,
# wrapper/UNION/JOIN-agnostic. 'fisheries_landings' is a substring of
# 'fisheries_landings_divisions', so the pitfall to pin is a query that names
# only the divisions table (even twice) must NOT be refused.

def test_landings_refusal_flags_sum_across_both_tables():
    from surimi_data_mcp.engine.sql_tools import landings_double_count_refusal
    r = landings_double_count_refusal(
        "SELECT SUM(landings) AS total FROM ("
        "SELECT landings FROM hive.fisheries.fisheries_landings WHERE fao_area='27' AND year=2008 "
        "UNION ALL "
        "SELECT landings FROM hive.fisheries.fisheries_landings_divisions WHERE year=2008) t")
    assert r  # a non-empty refusal reason
    assert "fisheries_landings" in r and "fisheries_landings_divisions" in r


def test_landings_refusal_allows_area_table_alone():
    from surimi_data_mcp.engine.sql_tools import landings_double_count_refusal
    assert landings_double_count_refusal(
        "SELECT SUM(landings) FROM hive.fisheries.fisheries_landings "
        "WHERE fao_area='27' AND year=2008") is None


def test_landings_refusal_allows_divisions_table_alone():
    from surimi_data_mcp.engine.sql_tools import landings_double_count_refusal
    assert landings_double_count_refusal(
        "SELECT SUM(landings) FROM hive.fisheries.fisheries_landings_divisions "
        "WHERE year=2008") is None


def test_landings_refusal_ignores_divisions_table_named_twice():
    # the substring pitfall: 'fisheries_landings' is a prefix of
    # 'fisheries_landings_divisions', so a self-join naming ONLY the
    # divisions table (twice) must not be mistaken for both tables.
    from surimi_data_mcp.engine.sql_tools import landings_double_count_refusal
    assert landings_double_count_refusal(
        "SELECT SUM(a.landings) FROM hive.fisheries.fisheries_landings_divisions a "
        "JOIN hive.fisheries.fisheries_landings_divisions b ON a.year = b.year") is None


def test_landings_refusal_ignores_non_magnitude_query_on_both_tables():
    # row lookups / no aggregate on both tables are fine -- only a magnitude
    # aggregate co-occurring with both tokens is refused.
    from surimi_data_mcp.engine.sql_tools import landings_double_count_refusal
    assert landings_double_count_refusal(
        "SELECT * FROM hive.fisheries.fisheries_landings a "
        "JOIN hive.fisheries.fisheries_landings_divisions b ON a.fao_area = b.fao_area "
        "LIMIT 10") is None


def test_eav_refusal_still_works_alongside_landings_guard():
    # the pre-existing EAV guard is untouched by adding the landings one.
    from surimi_data_mcp.engine.sql_tools import eav_magnitude_refusal
    r = eav_magnitude_refusal(
        "SELECT SUM(variable_value) FROM hive.eu_dcf.aer_social_data_2017_2020 "
        "WHERE variable_code='socemploy' AND year=2020")
    assert r


# ── cube fallback: serve cube-only tables from the DuckDB cube ────────────────
# query_data runs against Postgres, which on EDITO holds only the analytic subset.
# A table outside it lives only in the cube (the superset). When Postgres
# reports table-not-found for a table the cube DOES have, the server falls back to
# the cube, so that table is not a false "can't retrieve" refusal. The cube-served
# result is still HMAC-signed, with provenance that names the cube as the backend.
_BIOMASS_FQN = "hive.surimi.surimi_empirical_biomass_surveys_western_mediterranean"
_BIOMASS_CUBE = "hive_surimi_surimi_empirical_biomass_surveys_western_mediterranean"


class _MissClient:
    """A primary client that reports the biomass table missing (EDITO Postgres)."""

    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def execute(self, sql: str, limit: int = 500) -> dict[str, Any]:
        self.calls.append((sql, limit))
        return {
            "columns": [], "rows": [], "row_count": 0, "truncated": False,
            "error": f'relation "surimi.surimi_empirical_biomass_surveys_western_mediterranean" does not exist',
        }


class _FakeCube:
    def __init__(self, names, rows):
        self._names = list(names)
        self._rows = rows
        self.queries: list[str] = []

    def datasets(self):
        return list(self._names)

    def query(self, sql: str):
        self.queries.append(sql)
        return list(self._rows)


# ── CubeFallbackClient: primary SQL client + DuckDB-cube fallback ─────────────
def test_cube_fallback_client_serves_cube_on_table_miss():
    from surimi_data_mcp.engine.sql_tools import CubeFallbackClient

    cube = _FakeCube(names=[_BIOMASS_CUBE], rows=[{"year": 2020, "biomass_kt": 12.5}])
    c = CubeFallbackClient(_MissClient(), cube)
    res = c.execute(f"SELECT * FROM {_BIOMASS_FQN} LIMIT 3", limit=3)
    assert res["error"] is None
    assert res["rows"] == [{"year": 2020, "biomass_kt": 12.5}]
    assert res["row_count"] == 1
    assert res["served_by"] == "duckdb-cube"
    # the cube was queried, with the flat sanitized table name
    assert _BIOMASS_CUBE in res["cube_sql"]
    assert cube.queries and _BIOMASS_CUBE in cube.queries[0]


def test_cube_fallback_client_passthrough_on_success():
    from surimi_data_mcp.engine.sql_tools import CubeFallbackClient

    cube = _FakeCube(names=[_BIOMASS_CUBE], rows=[{"a": 1}])
    c = CubeFallbackClient(_FakeClient(), cube)
    res = c.execute("SELECT 1", limit=1)
    assert "served_by" not in res  # a successful primary query is never overridden
    assert cube.queries == []


def test_cube_fallback_client_passthrough_on_column_error():
    from surimi_data_mcp.engine.sql_tools import CubeFallbackClient

    class _ColErr:
        def execute(self, sql: str, limit: int = 500) -> dict[str, Any]:
            return {"columns": [], "rows": [], "row_count": 0,
                    "error": "Column 'foo' cannot be resolved"}

    cube = _FakeCube(names=[_BIOMASS_CUBE], rows=[{"a": 1}])
    c = CubeFallbackClient(_ColErr(), cube)
    res = c.execute(f"SELECT foo FROM {_BIOMASS_FQN}", limit=5)
    assert "served_by" not in res  # a column error is not a table-not-found
    assert cube.queries == []
    assert "Column" in res["error"]


def test_cube_fallback_client_passthrough_when_table_absent_from_cube():
    from surimi_data_mcp.engine.sql_tools import CubeFallbackClient

    cube = _FakeCube(names=["hive_eu_dcf_aer_capacity"], rows=[{"a": 1}])  # no biomass
    c = CubeFallbackClient(_MissClient(), cube)
    res = c.execute(f"SELECT * FROM {_BIOMASS_FQN}", limit=5)
    assert "served_by" not in res  # nothing referenced resolved to a cube table
    assert cube.queries == []
    assert "does not exist" in res["error"]


# ── make_query_data_signed: honest provenance from the served_by tag ──────────
def test_query_data_signed_cube_provenance_from_tag():
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed, CubeFallbackClient

    cube = _FakeCube(names=[_BIOMASS_CUBE], rows=[{"year": 2020, "biomass_kt": 12.5}])
    fn = make_query_data_signed(
        client_factory=lambda: CubeFallbackClient(_MissClient(), cube),
        backend_id="trino://hive@127.0.0.1:8081",
    )
    out = fn(sql=f"SELECT * FROM {_BIOMASS_FQN}", limit=3)
    prov = out["receipt"]["provenance"]
    assert prov["backend"] == "duckdb-cube"
    assert prov["human_check"]["engine"] == "duckdb-cube"
    assert _BIOMASS_CUBE in prov["human_check"]["sql"]
    # internal routing tags are stripped from the signed value
    assert "served_by" not in out["value"]
    assert "cube_sql" not in out["value"]


def test_query_data_signed_cube_result_verifies():
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed, CubeFallbackClient

    cube = _FakeCube(names=[_BIOMASS_CUBE], rows=[{"year": 2020, "biomass_kt": 12.5}])
    fn = make_query_data_signed(
        client_factory=lambda: CubeFallbackClient(_MissClient(), cube))
    out = fn(sql=f"SELECT * FROM {_BIOMASS_FQN}", limit=3)
    v = verify_receipt(out["receipt"], secret=SECRET)
    assert v["verified"] is True


def test_query_data_signed_primary_provenance_without_cube():
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed

    # No cube wrapper -> unchanged behaviour: the table-not-found error stands,
    # provenance stays the primary backend.
    fn = make_query_data_signed(
        client_factory=_MissClient, backend_id="trino://hive@127.0.0.1:8081")
    out = fn(sql=f"SELECT * FROM {_BIOMASS_FQN}", limit=5)
    assert out["receipt"]["provenance"]["backend"] == "trino://hive@127.0.0.1:8081"
    assert "does not exist" in out["value"]["error"]


# ── cube lineage in the fallback path ────────────────────────────────────────

class _FakeCubeClient(_FakeClient):
    """Mimics CubeFallbackClient: tags the result as served by the cube."""

    def execute(self, sql: str, limit: int = 500) -> dict[str, Any]:
        out = super().execute(sql, limit)
        out["served_by"] = "duckdb-cube"
        out["cube_sql"] = "SELECT country, n FROM hive_surimi_x"
        return out


def test_cube_served_receipt_records_cube_sha256(tmp_path, monkeypatch):
    """A number produced BY the cube must carry that cube's sha256, or a human
    cannot tell which cube produced it -- which is the point of the receipt."""
    import hashlib
    from surimi_data_mcp.provenance import lineage
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed

    cube = tmp_path / "cube.duckdb"
    cube.write_bytes(b"fake-cube-bytes")
    expected = hashlib.sha256(b"fake-cube-bytes").hexdigest()

    monkeypatch.setenv("SURIMI_CUBE_DB", str(cube))
    monkeypatch.setattr(lineage, "_CUBE_SHA", None, raising=False)
    monkeypatch.setattr(lineage, "_CUBE_SHA_DONE", False, raising=False)

    fn = make_query_data_signed(
        client_factory=_FakeCubeClient,
        backend_id="trino://hive@127.0.0.1:8081",
    )
    out = fn(sql="SELECT 1", limit=1)
    hc = out["receipt"]["provenance"]["human_check"]

    assert hc["engine"] == "duckdb-cube"
    assert hc["cube_sha256"] == expected


def test_trino_served_receipt_has_no_cube_sha256(tmp_path, monkeypatch):
    """The Trino path never touches the cube, so it must NOT claim a cube hash."""
    from surimi_data_mcp.provenance import lineage
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed

    cube = tmp_path / "cube.duckdb"
    cube.write_bytes(b"fake-cube-bytes")
    monkeypatch.setenv("SURIMI_CUBE_DB", str(cube))
    monkeypatch.setattr(lineage, "_CUBE_SHA", None, raising=False)
    monkeypatch.setattr(lineage, "_CUBE_SHA_DONE", False, raising=False)

    fn = make_query_data_signed(
        client_factory=_FakeClient,
        backend_id="trino://hive@127.0.0.1:8081",
    )
    out = fn(sql="SELECT 1", limit=1)
    assert out["receipt"]["provenance"]["human_check"]["cube_sha256"] is None
