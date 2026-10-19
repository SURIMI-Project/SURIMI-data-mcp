"""list_datasets must not advertise a dataset the cube cannot serve.

Measured 2026-09-21. list_datasets returns catalog.DATASETS, a hand-written
list, while aggregate validates against the real DuckDB cube. Nothing
reconciled the two, so the server advertised 28 datasets and three of them
answered "unknown dataset 'eu-dcf-catches'" on every call.

Two costs, both seen in benchmark transcripts. On Q-E2-2 Muse spent 11 tool
calls hammering eu-dcf-catches and its Trino FQN, got an error every time and
refused. On Q-E10-1 it listed the advertised datasets faithfully and a judge
graded the answer FABRICATED for naming datasets that do not resolve. The
model was repeating the server's own catalogue. The server was lying to it.

The reconciliation happens against the live cube rather than a second
hand-written list, so a deployment whose cube DOES hold these tables marks
nothing and is unchanged.
"""
from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.tools import common


def _resolve(name):
    """Stand-in for common.resolve_cube_dataset, which is handed the catalogue
    `table` and returns the cube's sanitized stem for it."""
    return name.replace(".", "_")


def test_a_dataset_the_cube_holds_is_not_marked():
    rows = [{"id": "aer-capacity", "table": "hive.eu_dcf.aer_capacity"}]
    out = catalog.mark_unservable(rows, _resolve, {"hive_eu_dcf_aer_capacity"})
    assert out[0].get("cube_servable") is not False
    assert "not_servable" not in out[0]


def test_a_dataset_the_cube_lacks_is_marked_with_a_reason():
    rows = [{"id": "eu-dcf-catches", "table": "hive.eu_dcf.eu_dcf_catches"}]
    out = catalog.mark_unservable(rows, _resolve, {"hive_eu_dcf_aer_capacity"})
    assert out[0]["cube_servable"] is False
    reason = out[0]["not_servable"]
    assert "eu-dcf-catches" in reason
    # The reason must tell the model what to do next, and the next thing is to
    # try query_data, not to give up. See the deployed-server note below.
    assert "query_data" in reason


def test_the_input_rows_are_not_mutated():
    """catalog.DATASETS is module state; marking a request must not edit it."""
    rows = [{"id": "eu-dcf-catches", "table": "hive.eu_dcf.eu_dcf_catches"}]
    catalog.mark_unservable(rows, _resolve, set())
    assert "cube_servable" not in rows[0]


def test_an_unavailable_cube_marks_nothing():
    """Fail open. Claiming a real dataset is unservable is worse than silence."""
    def boom(_name):
        raise RuntimeError("cube not built")
    rows = [{"id": "aer-capacity", "table": "hive.eu_dcf.aer_capacity"}]
    out = catalog.mark_unservable(rows, boom, {"hive_eu_dcf_aer_capacity"})
    assert out[0].get("cube_servable") is not False


def test_no_cube_names_marks_nothing():
    """An empty cube list means 'not known yet', never 'nothing is servable'."""
    rows = [{"id": "aer-capacity", "table": "hive.eu_dcf.aer_capacity"}]
    out = catalog.mark_unservable(rows, _resolve, set())
    assert out[0].get("cube_servable") is not False


# --- the flag must not claim data is gone when SQL still reaches it ---------
# Found 2026-09-21 by checking the DEPLOYED server. Seven catalogued datasets
# fail `aggregate` there with "unknown dataset", and they split two ways:
# five have no relation in the backend at all, but `fisheries-landings` and
# `fisheries-landings-divisions` are present and populated, returning 971,935
# and 566,113 rows through `query_data`. Both groups are absent from the CUBE,
# which is all this reconciliation can see, so the first draft marked all seven
# with "every query against it fails. Do not query it."
#
# For the landings pair that sentence is false, and it is the specific failure
# the system prompt calls the worst possible answer: reporting no data for data
# that exists. It would have suppressed a working path to a million rows.
#
# The flag can only honestly say what it checked: the cube cannot serve it, so
# the aggregate family fails. Whether query_data reaches the table is a
# different question, and query_data's own error answers it.

def test_the_reason_does_not_claim_every_query_fails():
    rows = [{"id": "fisheries-landings", "table": "hive.fao.fisheries_landings"}]
    out = catalog.mark_unservable(rows, _resolve, {"other"})
    reason = out[0]["not_servable"]
    assert "every query against it fails" not in reason
    assert "Do not query it" not in reason


def test_the_reason_names_the_tools_that_actually_fail():
    rows = [{"id": "fisheries-landings", "table": "hive.fao.fisheries_landings"}]
    reason = catalog.mark_unservable(rows, _resolve, {"other"})[0]["not_servable"]
    assert "aggregate" in reason
    assert "query_data" in reason


def test_the_flag_is_named_for_what_it_checked():
    """`servable: false` reads as "unusable". The check only knows about the
    cube, so the key says cube."""
    rows = [{"id": "fisheries-landings", "table": "hive.fao.fisheries_landings"}]
    out = catalog.mark_unservable(rows, _resolve, {"other"})
    assert out[0]["cube_servable"] is False
    assert "servable" not in out[0]
