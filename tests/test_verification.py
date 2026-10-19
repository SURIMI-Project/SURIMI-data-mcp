"""Tests for verification.verify_and_recompute: signature-first verify + recompute."""
import duckdb
import pytest

from surimi_data_mcp.engine import aggregate as agg_mod
from surimi_data_mcp.provenance import lineage
from surimi_data_mcp.provenance import verification
from surimi_data_mcp.engine.cube import Cube


@pytest.fixture(autouse=True)
def _reset_lineage():
    yield
    lineage.reset()


@pytest.fixture()
def cube(tmp_path, monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", str(tmp_path / "absent.json"))
    monkeypatch.delenv("SURIMI_CUBE_DB", raising=False)
    lineage.reset()
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ds1 (country VARCHAR, v INTEGER)")
    con.execute("INSERT INTO ds1 VALUES ('NO', 1), ('NO', 2), ('SE', 5)")
    return Cube(con)


def _receipt(cube):
    return agg_mod.aggregate(cube, "ds1", [{"col": "v", "fn": "sum"}],
                             dimensions=["country"])["receipt"]


def test_recompute_match(cube):
    out = verification.verify_and_recompute(
        _receipt(cube), recompute=True, cube_factory=lambda: cube)
    assert out["verified"] is True
    assert out["recompute_supported"] is True
    assert out["match"] is True
    assert out["claimed"] == out["recomputed"]        # the rows


def test_recompute_detects_drift(cube):
    r = _receipt(cube)
    cube.con.execute("UPDATE ds1 SET v = 999 WHERE country = 'NO'")  # data changed
    out = verification.verify_and_recompute(
        r, recompute=True, cube_factory=lambda: cube)
    assert out["verified"] is True                    # signature still fine
    assert out["match"] is False                      # numbers no longer reproduce
    assert out["mismatches"]                          # compact diff present


def test_tampered_receipt_never_recomputes(cube):
    r = _receipt(cube)
    bad = {**r, "output_value": {**r["output_value"], "row_count": 999}}
    out = verification.verify_and_recompute(
        bad, recompute=True, cube_factory=lambda: cube)
    assert out["verified"] is False
    assert out["recompute_supported"] is False        # refused: signature first
    assert "match" not in out


def test_unsupported_tool_id(cube):
    from surimi_data_mcp.provenance.receipts import issue_receipt
    nc = issue_receipt(tool_id="nc_top_regions", input_params={"f": 1},
                       output_value=[1, 2], secret=b"test-secret")
    out = verification.verify_and_recompute(nc, recompute=True,
                                            cube_factory=lambda: cube)
    assert out["verified"] is True
    assert out["recompute_supported"] is False


def test_no_recompute_is_signature_only(cube):
    out = verification.verify_and_recompute(_receipt(cube), recompute=False,
                                            cube_factory=lambda: cube)
    assert out["verified"] is True
    assert "match" not in out


def test_recompute_decimal_columns_match(cube):
    """SUM over DECIMAL returns Decimal from DuckDB, but the receipt stored the
    _coerce-d string form. Both sides must be normalized through the same
    coercion or unchanged data would falsely report a mismatch."""
    cube.con.execute("CREATE TABLE ds2 (country VARCHAR, v DECIMAL(10,2))")
    cube.con.execute("INSERT INTO ds2 VALUES ('NO', 1.50), ('NO', 2.25), ('SE', 5.00)")
    r = agg_mod.aggregate(cube, "ds2", [{"col": "v", "fn": "sum"}],
                          dimensions=["country"])["receipt"]
    out = verification.verify_and_recompute(
        r, recompute=True, cube_factory=lambda: cube)
    assert out["verified"] is True
    assert out["recompute_supported"] is True
    assert out["match"] is True
    assert out["claimed"] == out["recomputed"]


def test_recompute_query_data(monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")

    class FakeClient:
        def execute(self, sql, limit=500):
            return {"rows": [{"n": 3}], "row_count": 1}

    from surimi_data_mcp.engine.sql_tools import make_query_data_signed
    qd = make_query_data_signed(lambda: FakeClient(), backend_id="trino://test")
    r = qd("SELECT COUNT(*) AS n FROM t")["receipt"]
    out = verification.verify_and_recompute(
        r, recompute=True, query_client_factory=lambda: FakeClient())
    assert out["recompute_supported"] is True and out["match"] is True


def test_recompute_query_data_cube_served_matches(monkeypatch):
    """A biomass receipt served from the cube (Postgres lacked the table) must
    still recompute to a MATCH. Recompute must run through the SAME cube fallback
    as issuance, not the raw primary, which would miss the table and mismatch."""
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed, CubeFallbackClient

    class _MissPrimary:
        def execute(self, sql, limit=500):
            return {"columns": [], "rows": [], "row_count": 0,
                    "error": 'relation "surimi.x" does not exist'}

    class _Cube:
        def datasets(self):
            return ["hive_surimi_x"]

        def query(self, sql):
            return [{"n": 7}]

    fallback = lambda: CubeFallbackClient(_MissPrimary(), _Cube())
    qd = make_query_data_signed(fallback, backend_id="trino://test")
    r = qd("SELECT n FROM hive.surimi.x")["receipt"]
    assert r["provenance"]["backend"] == "duckdb-cube"  # issued from the cube
    out = verification.verify_and_recompute(
        r, recompute=True, query_client_factory=fallback)
    assert out["recompute_supported"] is True
    assert out["match"] is True


def _signed_query_data_receipt(sql, rows, monkeypatch):
    """Issue a signed query_data receipt via a fake client (issuing does NOT
    guard the SQL either, that is exactly the point of the recompute gate)."""
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")

    class IssueClient:
        def execute(self, sql, limit=500):
            return {"rows": rows, "row_count": len(rows)}

    from surimi_data_mcp.engine.sql_tools import make_query_data_signed
    qd = make_query_data_signed(lambda: IssueClient(), backend_id="trino://test")
    return qd(sql)["receipt"]


def test_recompute_rejects_non_select_sql(monkeypatch):
    """A signed receipt storing DELETE must never reach the engine on
    recompute: guard fires before execute, message stays meaningful."""
    r = _signed_query_data_receipt("DELETE FROM t", [{"n": 1}], monkeypatch)

    calls = []

    class TrackingClient:
        def execute(self, sql, limit=500):
            calls.append(sql)
            return {"rows": [{"n": 1}], "row_count": 1}

    out = verification.verify_and_recompute(
        r, recompute=True, query_client_factory=lambda: TrackingClient())
    assert out["verified"] is True                    # signature is fine
    assert out["match"] is False                      # rejection = no reproduce
    assert "only SELECT" in out["mismatches"][0]["error"]
    assert calls == []                                # engine never touched


@pytest.mark.parametrize("sql", [
    "WITH a AS (SELECT 1 AS n) SELECT * FROM a",
    "SELECT 1 AS n UNION SELECT 1 AS n",
])
def test_recompute_allows_cte_and_union(sql, monkeypatch):
    r = _signed_query_data_receipt(sql, [{"n": 1}], monkeypatch)

    calls = []

    class TrackingClient:
        def execute(self, sql, limit=500):
            calls.append(sql)
            return {"rows": [{"n": 1}], "row_count": 1}

    out = verification.verify_and_recompute(
        r, recompute=True, query_client_factory=lambda: TrackingClient())
    assert calls == [sql]                             # guard let it through
    assert out["match"] is True


def test_recompute_generic_error_is_sanitized(monkeypatch):
    """DB driver exceptions leak schema/host/SQL fragments; the public route
    must return a generic message and keep details in server logs."""
    r = _signed_query_data_receipt("SELECT 1 AS n", [{"n": 1}], monkeypatch)

    class ExplodingClient:
        def execute(self, sql, limit=500):
            raise RuntimeError("trino://secret-host:8443/hive/schema.t blew up")

    out = verification.verify_and_recompute(
        r, recompute=True, query_client_factory=lambda: ExplodingClient())
    assert out["match"] is False
    assert out["mismatches"][0]["error"] == "recompute failed (see server logs)"


def test_recompute_row_order_is_not_drift(monkeypatch):
    """Engines without an explicit ORDER BY return rows in nondeterministic
    order (DuckDB GROUP BY does this between runs). Same values in a different
    order must NOT be reported as a mismatch."""
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")

    class IssueClient:
        def execute(self, sql, limit=500):
            return {"rows": [{"c": "NO", "n": 3}, {"c": "SE", "n": 5}],
                    "row_count": 2}

    class ReorderedClient:
        def execute(self, sql, limit=500):
            return {"rows": [{"c": "SE", "n": 5}, {"c": "NO", "n": 3}],
                    "row_count": 2}

    from surimi_data_mcp.engine.sql_tools import make_query_data_signed
    qd = make_query_data_signed(lambda: IssueClient(), backend_id="trino://test")
    r = qd("SELECT c, COUNT(*) AS n FROM t GROUP BY c")["receipt"]
    out = verification.verify_and_recompute(
        r, recompute=True, query_client_factory=lambda: ReorderedClient())
    assert out["match"] is True


def test_model_receipt_says_it_was_verified_by_signature_only(cube):
    """A model receipt verifies, but nothing re-executed it.

    The audit page told the reader to "use the verify tool in the chat
    instead" for these, implying the chat tool could recompute them. It
    cannot: no recompute path exists for model_* on either server. Saying so
    in the answer is the difference between a checked number and a signed one.
    """
    from surimi_data_mcp.provenance.receipts import issue_receipt
    r = issue_receipt(tool_id="model_trend", input_params={"f": 1},
                      output_value=[1, 2], secret=b"test-secret")
    out = verification.verify_and_recompute(r, recompute=True,
                                            cube_factory=lambda: cube)
    assert out["verified"] is True
    assert out["recompute_supported"] is False
    assert out["verification_method"] == "signature_only"
    assert "recompute" in out["recompute_note"].lower()


def test_aggregate_receipt_reports_a_recomputed_method(cube):
    out = verification.verify_and_recompute(_receipt(cube), recompute=True,
                                            cube_factory=lambda: cube)
    assert out["verification_method"] == "signature_and_recompute"


def test_signature_only_when_recompute_not_requested(cube):
    """Not asking for a recompute must not be reported as one having happened."""
    out = verification.verify_and_recompute(_receipt(cube), recompute=False,
                                            cube_factory=lambda: cube)
    assert out["verification_method"] == "signature_only"


def test_verification_method_is_reported_on_a_failed_signature(cube):
    """The field describes what the SERVER can do, not how one check came out.

    A tampered receipt returned recompute_supported without
    verification_method, so a caller parsing the reply saw the field appear and
    vanish depending on the outcome. The model server reports it on every path;
    these two shapes have to match, because a reviewer asked whether a number
    can be REPRODUCED and the answer must not differ by which server replied.
    """
    r = _receipt(cube)
    r["output_value"] = [{"tampered": True}]
    out = verification.verify_and_recompute(r, recompute=True,
                                            cube_factory=lambda: cube)
    assert out["verified"] is False
    assert out["verification_method"] == "signature_only"


def test_verification_method_is_reported_when_no_secret_is_configured(cube, monkeypatch):
    r = _receipt(cube)          # needs the secret; build it before clearing
    monkeypatch.delenv("SURIMI_RECEIPT_SECRET", raising=False)
    out = verification.verify_and_recompute(r, recompute=True,
                                            cube_factory=lambda: cube)
    assert out["reason"] == "secret_not_configured"
    assert out["verification_method"] == "signature_only"
