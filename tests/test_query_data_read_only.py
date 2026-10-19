"""query_data can only read (2026-10-12).

query_data handed its SQL to PostgreSQL unchanged, on a public endpoint, as a role that owns the
tables. A transaction left open was rolled back, but SQL that committed itself ("...; COMMIT") or a
data-modifying CTE could change the copy the server answers from, and the result would have come
back with a valid receipt. Two walls now: the SQL must be one SELECT with no write anywhere in its
tree, and the PostgreSQL session itself is read-only with a statement time limit.
"""
from __future__ import annotations

import pytest

from surimi_data_mcp.engine import sql_tools
from surimi_data_mcp.engine.sql_tools import read_only_refusal
from surimi_data_mcp.tools import figures

READS = [
    "SELECT country, SUM(landings) FROM hive.fisheries.fisheries_landings GROUP BY country",
    "WITH t AS (SELECT * FROM hive.eu_dcf.aer_capacity) SELECT COUNT(*) FROM t",
    "SELECT 1 UNION ALL SELECT 2",
    "SHOW COLUMNS FROM hive.eu_dcf.aer_capacity",
    "select * from v_sales_by_species_year limit 5",
]

WRITES = [
    "DROP TABLE eu_dcf.aer_capacity",
    "SELECT 1; DROP TABLE eu_dcf.aer_capacity",
    "WITH d AS (DELETE FROM eu_dcf.aer_capacity RETURNING *) SELECT * FROM d",
    "COMMIT",
    "SELECT 1; COMMIT; DELETE FROM eu_dcf.aer_capacity",
    "SET default_transaction_read_only = off",
    "INSERT INTO eu_dcf.aer_capacity VALUES (1)",
    "UPDATE eu_dcf.aer_capacity SET year = 0",
    "CREATE TABLE x (a int)",
    "",
    "this is not sql at all ((",
]


@pytest.mark.parametrize("sql", READS)
def test_a_single_read_passes(sql):
    assert read_only_refusal(sql) is None


@pytest.mark.parametrize("sql", WRITES)
def test_anything_that_could_write_is_refused(sql):
    msg = read_only_refusal(sql)
    assert msg and "read-only" in msg


def test_query_data_refuses_before_anything_runs(monkeypatch):
    from surimi_data_mcp import server

    ran = []
    monkeypatch.setattr(figures, "_query_data_signed", lambda **kw: ran.append(kw) or {"receipt": None})
    out = figures.query_data("SELECT 1; DROP TABLE eu_dcf.aer_capacity")
    assert ran == []
    assert out["receipt"] is None and "read-only" in out["value"]["error"]


def test_the_postgres_session_is_read_only_with_a_time_limit(monkeypatch):
    import psycopg2

    from surimi_data_mcp.engine.databases import PostgresClient

    seen = {}
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **kw: seen.update(args=a, kw=kw) or object())
    PostgresClient("postgresql://u:p@h:5432/d")._connection()
    options = seen["kw"].get("options", "")
    assert "default_transaction_read_only=on" in options
    assert "statement_timeout=" in options


def test_receipt_recompute_uses_the_same_wall():
    from surimi_data_mcp.provenance.verification import RecomputeRejected, _assert_select_only

    with pytest.raises(RecomputeRejected):
        _assert_select_only("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d")
    _assert_select_only("SELECT 1")


def test_the_refusal_names_what_is_allowed():
    msg = read_only_refusal("DROP TABLE t")
    assert "SELECT" in msg and sql_tools.READ_ONLY_REFUSAL_PREFIX in msg
