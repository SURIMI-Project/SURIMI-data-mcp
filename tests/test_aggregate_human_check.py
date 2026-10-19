"""aggregate() receipts must carry a signed human_check block.

The whole point: ``_aggregate_impl`` puts ``human_check`` inside
``value["provenance"]``, and the public ``aggregate()`` wrapper signs
``value.get("provenance")`` into the receipt, so the copy-pasteable SQL and
source lineage are tamper-evident, not decorative.
"""
import duckdb
import pytest

from surimi_data_mcp.engine import aggregate as agg_mod
from surimi_data_mcp.provenance import lineage
from surimi_data_mcp.engine.cube import Cube
from surimi_data_mcp.provenance.receipts import verify_receipt


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


def test_receipt_carries_signed_human_check(cube):
    out = agg_mod.aggregate(cube, "ds1", [{"col": "v", "fn": "sum"}],
                            dimensions=["country"],
                            filters=[{"col": "country", "op": "=", "value": "NO"}])
    hc = out["receipt"]["provenance"]["human_check"]
    assert "SELECT" in hc["sql"] and "'NO'" in hc["sql"] and "?" not in hc["sql"]
    assert hc["engine"] == "duckdb-cube"
    assert hc["source"] is None                      # no manifest -> honest null
    # human_check is INSIDE the signed payload:
    v = verify_receipt(out["receipt"], secret=b"test-secret")
    assert v["verified"] is True
    tampered = {**out["receipt"],
                "provenance": {**out["receipt"]["provenance"],
                               "human_check": {**hc, "sql": "SELECT 666"}}}
    assert verify_receipt(tampered, secret=b"test-secret")["verified"] is False


def test_inlined_sql_reproduces_the_numbers(cube):
    out = agg_mod.aggregate(cube, "ds1", [{"col": "v", "fn": "sum"}],
                            dimensions=["country"])
    sql = out["receipt"]["provenance"]["human_check"]["sql"]
    rerun = cube.con.execute(sql).fetchall()         # human's copy-paste path
    by_country = {r[0]: r[1] for r in rerun}
    claimed = {r["country"]: r["v_sum"] for r in out["value"]["rows"]}
    assert by_country == claimed


def test_nonfinite_filter_value_rejected_early(cube):
    """NaN/inf filter values are meaningless; they must be rejected with the
    tool's structured AggregateError BEFORE any query runs, never escape as a
    plain ValueError from receipt construction after the work is done."""
    from surimi_data_mcp.engine.aggregate import AggregateError

    with pytest.raises(AggregateError, match="non-finite"):
        agg_mod.aggregate(cube, "ds1", [{"col": "v", "fn": "sum"}],
                          filters=[{"col": "v", "op": "=",
                                    "value": float("nan")}])
    with pytest.raises(AggregateError, match="non-finite"):
        agg_mod.aggregate(cube, "ds1", [{"col": "v", "fn": "sum"}],
                          having=[{"agg": "v_sum", "op": ">",
                                   "value": float("inf")}])


def test_nonfinite_in_list_element_rejected_early(cube):
    from surimi_data_mcp.engine.aggregate import AggregateError

    with pytest.raises(AggregateError, match="non-finite"):
        agg_mod.aggregate(cube, "ds1", [{"col": "v", "fn": "sum"}],
                          filters=[{"col": "v", "op": "in",
                                    "value": [1.0, float("-inf")]}])
