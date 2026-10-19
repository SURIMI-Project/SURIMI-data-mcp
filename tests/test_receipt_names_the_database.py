"""A query_data receipt names the database that ran the query (2026-10-13).

Every primary-served receipt said trino://hive@127.0.0.1:8081, a fixed string, while on EDITO the
query runs on PostgreSQL. The receipt is signed and shown to people as the way to re-run the
number, so it now names the client that answered. The name never carries a user or password.
"""
import pytest

from surimi_data_mcp.engine.databases import PostgresClient, TrinoClient
from surimi_data_mcp.engine.sql_tools import CubeFallbackClient, make_query_data_signed


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-receipt-backend-secret")


def test_postgres_is_named_without_its_credentials():
    name = PostgresClient("postgresql://reader:s3cret-pw@db.example:5432/surimi").backend_id
    assert name == "postgresql://db.example:5432/surimi"
    assert "reader" not in name and "s3cret" not in name


def test_trino_is_named_by_catalog_host_and_port():
    assert TrinoClient(host="trino.example", port=8443, catalog="hive").backend_id == \
        "trino://hive@trino.example:8443"


def test_the_cube_fallback_client_carries_its_primary_name():
    primary = PostgresClient("postgresql://u:p@db:5432/surimi")
    assert CubeFallbackClient(primary, cube=None).backend_id == "postgresql://db:5432/surimi"


class _Postgresish:
    backend_id = "postgresql://db:5432/surimi"

    def execute(self, sql, limit=500):
        return {"columns": ["n"], "rows": [[1]], "row_count": 1, "truncated": False, "error": None}


def test_the_receipt_names_the_client_that_answered():
    qd = make_query_data_signed(lambda: _Postgresish(), backend_id="trino://hive@127.0.0.1:8081")
    out = qd(sql="SELECT 1 AS n")
    prov = out["receipt"]["provenance"]
    assert prov["backend"] == "postgresql://db:5432/surimi"
    assert prov["human_check"]["engine"] == "postgresql://db:5432/surimi"
