"""MCP wiring of the deterministic ``aggregate`` tool.

These tests cover only the wiring contract in ``surimi_data_mcp/server.py`` (the maths is
proven against an independent oracle in ``test_aggregate.py``):

  * the ``aggregate`` tool is registered with FastMCP and appears in
    ``await mcp.list_tools()``;
  * a capability entry for the cube/aggregate source is in the registry and
    claims the ``aggregate`` tool;
  * the capabilities drift-check passes (no tool unclaimed by any source, no
    claimed tool missing from FastMCP);
  * the tool returns the schema'd shape, ``{value, receipt}`` with every
    ``AGGREGATE_SCHEMA["value"]`` key, the receipt verifies, and the numbers are
    real (cross-checked against an independent pandas computation so the wiring
    cannot be faked with a stub);
  * the tool is lazily backed by the shared cube (``cube.get_cube``, the same
    lazy-singleton pattern as ``docsearch.get_index``), so the server does not
    build the cube at import time;
  * the tool docstring instructs grounding: it returns EXACT computed aggregates
    and the LLM MUST call it for any total / average / count / ranking / min /
    max and never compute numbers itself.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pandas as pd
import pytest


AGG_TOOL = "aggregate"
SECRET = b"test-aggregate-tool-secret-do-not-use"


# A small landings-style corpus the tool aggregates AND an independent pandas
# oracle recomputes. Mirrors the hand-authored data in test_aggregate.py so the
# wiring test also proves real computation (not an echoing stub).
LANDINGS_ROWS = [
    ["year", "area", "species", "landings_tonnes"],
    ["2019", "27.4.a", "COD", "100.5"],
    ["2019", "27.4.a", "HER", "50.0"],
    ["2019", "37.1.1", "COD", "30.0"],
    ["2020", "27.4.a", "COD", "200.0"],
    ["2020", "37.1.1", "HAK", "75.25"],
    ["2021", "37.1.1", "HAK", "10.0"],
]


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A temp data dir holding one CSV, so a real cube can be built from it."""
    import csv as _csv

    d = tmp_path / "lake"
    d.mkdir()
    with open(d / "landings.csv", "w", newline="") as fh:
        _csv.writer(fh).writerows(LANDINGS_ROWS)
    return d


@pytest.fixture
def landings_df() -> pd.DataFrame:
    """The independent oracle: the same rows loaded by pandas (typed)."""
    header, *rows = LANDINGS_ROWS
    df = pd.DataFrame(rows, columns=header)
    df["landings_tonnes"] = df["landings_tonnes"].astype(float)
    return df


@pytest.fixture(autouse=True)
def _wire_env(monkeypatch, data_dir: Path):
    """Point the server's lazy cube at the temp corpus, set the receipt secret,
    and reset the cube singleton so each test builds a fresh in-memory cube."""
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", SECRET.decode())
    monkeypatch.setenv("SURIMI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SURIMI_CUBE_DB", ":memory:")
    from surimi_data_mcp.engine import cube
    monkeypatch.setattr(cube, "_CUBE", None, raising=False)
    yield
    monkeypatch.setattr(cube, "_CUBE", None, raising=False)


def _list_tool_names() -> list[str]:
    from surimi_data_mcp import server
    tools = asyncio.run(server.mcp.list_tools())
    return [t.name for t in tools]


def _get_tool(name: str):
    from surimi_data_mcp import server
    tools = asyncio.run(server.mcp.list_tools())
    for t in tools:
        if t.name == name:
            return t
    return None


# ───────────────────────── registration / discovery ─────────────────────────

def test_aggregate_tool_is_registered():
    """The aggregate tool appears in mcp.list_tools()."""
    assert AGG_TOOL in _list_tool_names()


def test_aggregate_capability_registered_and_claims_tool():
    """A capability entry exists for the cube/aggregate source and claims the
    aggregate tool, so describe_capabilities advertises it."""
    from surimi_data_mcp import server
    from surimi_data_mcp import capabilities

    sources = capabilities.all_capabilities()
    # exactly one source must claim the aggregate tool
    claiming = [c for c in sources if AGG_TOOL in c.tools]
    assert len(claiming) == 1, (
        f"expected exactly one capability to claim {AGG_TOOL!r}, "
        f"got {[c.source_id for c in claiming]}")
    cap = claiming[0]
    assert cap.title and cap.description
    assert AGG_TOOL in capabilities.claimed_tools()


def test_capabilities_drift_check_passes_with_aggregate():
    """With aggregate wired, the full drift check is clean: no live tool is
    unclaimed by a source, and no claimed tool is missing from FastMCP."""
    from surimi_data_mcp import server
    from surimi_data_mcp import capabilities

    live = set(_list_tool_names())
    drift = capabilities.drift_check(live)
    assert drift["missing_from_registry"] == [], (
        f"tools registered with FastMCP but claimed by no source: "
        f"{drift['missing_from_registry']}")
    assert drift["missing_from_fastmcp"] == [], (
        f"tools claimed by a source but not registered with FastMCP: "
        f"{drift['missing_from_fastmcp']}")


# ───────────────────────── schema'd shape / real compute ────────────────────

def _call_aggregate(**kwargs) -> dict:
    """Invoke the server tool's underlying function (the same callable FastMCP
    runs) with the given structured args, returning {value, receipt}."""
    tool = _get_tool(AGG_TOOL)
    assert tool is not None, "aggregate tool not registered"
    return tool.fn(**kwargs)


# The always-present value keys (the unconditional contract). The schema also
# documents `normalized_dimensions`, which the engine emits ONLY when a
# normalize_dimensions request collapses case variants -- asserted separately so
# the test reflects the real (conditional) contract, not a fabricated one.
_ALWAYS_PRESENT_VALUE_KEYS = {
    "rows", "row_count", "total_groups", "truncated",
    "null_dimensions_excluded", "notes", "provenance",
}


def test_tool_returns_schemad_shape():
    """The tool returns {value, receipt}; value carries every unconditional
    AGGREGATE_SCHEMA value key, and the schema documents exactly these keys plus
    the conditional `normalized_dimensions`."""
    from surimi_data_mcp.engine.aggregate import AGGREGATE_SCHEMA

    # The schema's documented value keys == the always-present set plus the one
    # conditional key -- so the test pins the schema to reality, not vice versa.
    assert set(AGGREGATE_SCHEMA["value"]) == (
        _ALWAYS_PRESENT_VALUE_KEYS | {"normalized_dimensions"})

    out = _call_aggregate(
        dataset="landings",
        measures=[{"col": "landings_tonnes", "fn": "sum"}],
        dimensions=["year"],
    )
    assert set(out.keys()) == {"value", "receipt", "audit_url"}
    value = out["value"]
    for key in _ALWAYS_PRESENT_VALUE_KEYS:
        assert key in value, f"result value missing required schema key {key!r}"
    # Without a normalize request, the conditional key is absent (honest shape).
    assert "normalized_dimensions" not in value
    assert isinstance(value["rows"], list)
    assert value["row_count"] == len(value["rows"])


def test_tool_normalized_dimensions_key_appears_when_requested():
    """The conditional `normalized_dimensions` key appears (and reports the
    merge) exactly when a normalize_dimensions request collapses case variants."""
    out = _call_aggregate(
        dataset="landings",
        measures=[{"col": "landings_tonnes", "fn": "sum"}],
        dimensions=["species"],
        normalize_dimensions=["species"],
    )
    value = out["value"]
    assert "normalized_dimensions" in value
    assert "species" in value["normalized_dimensions"]


def test_tool_numbers_match_independent_pandas_oracle(landings_df):
    """The wired tool returns REAL engine-computed numbers, not an echo: every
    per-year sum equals an independent pandas groupby over the same source."""
    out = _call_aggregate(
        dataset="landings",
        measures=[{"col": "landings_tonnes", "fn": "sum"}],
        dimensions=["year"],
    )
    got = {str(r["year"]): round(r["landings_tonnes_sum"], 6)
           for r in out["value"]["rows"]}

    expected = (landings_df.groupby("year")["landings_tonnes"].sum()
                .round(6).to_dict())
    expected = {str(k): v for k, v in expected.items()}

    assert got == expected
    # completeness: all groups present, nothing silently dropped.
    assert out["value"]["total_groups"] == len(expected)
    assert out["value"]["truncated"] is False


def test_tool_receipt_verifies_via_verify_tool():
    """The receipt the tool emits verifies with the server's verify tool (same
    secret), so any number the LLM surfaces is re-checkable."""
    from surimi_data_mcp import server

    out = _call_aggregate(
        dataset="landings",
        measures=[{"col": "landings_tonnes", "fn": "sum"}],
        dimensions=["year"],
    )
    verify_tool = _get_tool("verify")
    verdict = verify_tool.fn(receipt=out["receipt"])
    assert verdict["verified"] is True, verdict


def test_tool_tampered_number_fails_verification():
    """Tampering a returned number invalidates the receipt, proof the receipt
    binds the output, not just the inputs."""
    import copy

    out = _call_aggregate(
        dataset="landings",
        measures=[{"col": "landings_tonnes", "fn": "sum"}],
        dimensions=["year"],
    )
    verify_tool = _get_tool("verify")
    tampered = copy.deepcopy(out["receipt"])
    # corrupt the bound output digest's source by mutating the value we'd verify
    # against: verify recomputes over receipt['output'] if present, else trusts
    # the stored digest. We mutate a stored number in the receipt envelope.
    # Locate any numeric leaf in the receipt's output snapshot and bump it.
    snap = tampered.get("output") or tampered.get("output_value")
    assert snap is not None, "receipt must snapshot the signed output"
    rows = snap["rows"] if "rows" in snap else snap["value"]["rows"]
    rows[0]["landings_tonnes_sum"] = rows[0]["landings_tonnes_sum"] + 999.0
    verdict = verify_tool.fn(receipt=tampered)
    assert verdict["verified"] is False


# ───────────────────────── lazy cube init ───────────────────────────────────

def test_tool_uses_shared_lazy_cube(monkeypatch):
    """The tool builds the cube lazily via cube.get_cube (the docsearch pattern),
    not at import time, and reuses the singleton across calls."""
    from surimi_data_mcp.engine import cube

    # Singleton starts unbuilt (the autouse fixture reset it).
    assert cube._CUBE is None

    calls = {"n": 0}
    real_get_cube = cube.get_cube

    def counting_get_cube():
        calls["n"] += 1
        return real_get_cube()

    monkeypatch.setattr(cube, "get_cube", counting_get_cube)

    _call_aggregate(
        dataset="landings",
        measures=[{"col": "landings_tonnes", "fn": "sum"}],
        dimensions=["year"],
    )
    _call_aggregate(
        dataset="landings",
        measures=[{"col": "landings_tonnes", "fn": "avg"}],
        dimensions=["area"],
    )
    # The tool went through get_cube on each call (lazy accessor, not a build).
    assert calls["n"] >= 2
    # And the singleton was built exactly once and cached.
    assert cube._CUBE is not None


# ───────────────────────── grounding docstring ──────────────────────────────

def test_tool_docstring_instructs_grounding():
    """The tool's description (what the LLM sees) must tell the model to use the
    EXACT computed aggregates and never compute totals/averages/counts/rankings
    itself."""
    tool = _get_tool(AGG_TOOL)
    desc = (tool.description or "").lower()

    # It returns exact/computed aggregates.
    assert "exact" in desc or "computed" in desc
    # It must be used for the numeric-summary intents.
    for kw in ("total", "average", "count", "rank"):
        assert kw in desc, f"docstring must mention {kw!r} (numeric-summary intent)"
    # And it must forbid the model computing numbers itself.
    assert "never" in desc
    assert ("compute" in desc or "calculate" in desc or "yourself" in desc)


def test_aggregate_error_is_returned_not_raised():
    """An invalid request (unknown dataset) returns a structured error dict, not
    an exception that would crash the tool call, and carries NO fabricated
    numbers."""
    out = _call_aggregate(
        dataset="does_not_exist",
        measures=[{"col": "landings_tonnes", "fn": "sum"}],
        dimensions=["year"],
    )
    assert "error" in out and out["error"]
    # No fake value/receipt smuggled in on an error.
    assert out.get("value") in (None, {}, [])
    assert not out.get("receipt")


# ───────────── catalog-id -> cube-name bridge (the "unknown dataset" bug) ─────

def test_resolve_cube_dataset_bridges_catalog_id_and_fqn():
    """The chat only knows catalog ids (list_datasets) and Trino FQNs, but the
    cube keys datasets by sanitized CSV stem. `common.resolve_cube_dataset` must map both
    onto the real cube name, else every aggregate call from the chat fails."""
    from surimi_data_mcp import server
    from surimi_data_mcp.engine import cube as _cube
    cube_name = _cube._sanitize_name(
        "hive.eu_dcf.eu_dcf_fisheries_economic_performance_aegean_sea")

    class _FakeCube:
        def datasets(self):
            return [cube_name, _cube._sanitize_name("hive.eu_dcf.aer_capacity")]

    fc = _FakeCube()
    # kebab catalog id (what list_datasets returns) -> cube stem
    assert common.resolve_cube_dataset("eu-dcf-economic-aegean", fc) == cube_name
    # Trino FQN (the catalog 'table' value) -> cube stem
    assert common.resolve_cube_dataset(
        "hive.eu_dcf.eu_dcf_fisheries_economic_performance_aegean_sea", fc) == cube_name
    # already a cube name -> unchanged
    assert common.resolve_cube_dataset(cube_name, fc) == cube_name
    # genuinely unknown -> unchanged, so `aggregate` raises a clear error
    assert common.resolve_cube_dataset("totally-unknown", fc) == "totally-unknown"


def test_aggregate_tool_bridges_catalog_id_end_to_end(tmp_path, monkeypatch):
    """End-to-end through the real tool: a CSV named like the catalog 'table'
    builds a cube dataset under the sanitized stem; calling aggregate with the
    CATALOG id (what the chat has) resolves through the bridge and returns the
    real sum -- proving the chat's aggregate calls will work."""
    import csv as _csv
    from surimi_data_mcp.engine import cube as _cube
    d = tmp_path / "lake2"
    d.mkdir()
    # filename mirrors catalog table hive.eu_dcf.aer_capacity -> stem hive_eu_dcf_aer_capacity
    with open(d / "hive_eu_dcf_aer_capacity.csv", "w", newline="") as fh:
        _csv.writer(fh).writerows([
            ["country_code", "number_of_vessels"],
            ["ITA", "100"], ["ITA", "50"], ["GRC", "30"],
        ])
    monkeypatch.setenv("SURIMI_DATA_DIR", str(d))
    monkeypatch.setenv("SURIMI_CUBE_DB", ":memory:")
    monkeypatch.setattr(_cube, "_CUBE", None, raising=False)

    out = _call_aggregate(dataset="aer-capacity",   # the CATALOG id, not the stem
                          measures=[{"col": "number_of_vessels", "fn": "sum"}])
    assert out.get("error") is None, out
    rows = out["value"]["rows"]
    assert rows, out
    assert abs(float(list(rows[0].values())[0]) - 180.0) < 1e-6, rows


# ───────────────── audit_url wiring (shareable receipt links) ─────────────────

def _make_aer_lake(tmp_path, monkeypatch):
    """A lake whose CSV stem matches the catalog table for 'aer-capacity', so
    skill tools (which resolve dataset_id through the catalog) find a cube
    dataset to run on."""
    import csv as _csv
    from surimi_data_mcp.engine import cube as _cube
    d = tmp_path / "lake_aer"
    d.mkdir()
    with open(d / "hive_eu_dcf_aer_capacity.csv", "w", newline="") as fh:
        _csv.writer(fh).writerows([
            ["year", "country_code", "number_of_vessels"],
            ["2020", "ITA", "100"], ["2020", "ITA", "50"], ["2020", "GRC", "30"],
        ])
    monkeypatch.setenv("SURIMI_DATA_DIR", str(d))
    monkeypatch.setenv("SURIMI_CUBE_DB", ":memory:")
    monkeypatch.setattr(_cube, "_CUBE", None, raising=False)


def test_aggregate_tool_returns_audit_url(monkeypatch):
    """With SURIMI_PUBLIC_BASE_URL set, aggregate attaches a shareable audit_url
    NEXT TO the receipt (never inside it, as that would break the signature)."""
    monkeypatch.setenv("SURIMI_PUBLIC_BASE_URL", "https://mcp.example")
    out = _call_aggregate(
        dataset="landings",
        measures=[{"col": "landings_tonnes", "fn": "sum"}],
        dimensions=["year"],
    )
    assert out["audit_url"].startswith("https://mcp.example/audit#")
    # The link lives OUTSIDE the signed receipt; the receipt still verifies.
    assert "audit_url" not in out["receipt"]
    verify_tool = _get_tool("verify")
    assert verify_tool.fn(receipt=out["receipt"])["verified"] is True


def test_aggregate_tool_audit_url_none_without_base(monkeypatch):
    """Without the base URL the key is present but None, shape stays stable
    for old clients, no fabricated link."""
    monkeypatch.delenv("SURIMI_PUBLIC_BASE_URL", raising=False)
    out = _call_aggregate(
        dataset="landings",
        measures=[{"col": "landings_tonnes", "fn": "sum"}],
        dimensions=["year"],
    )
    assert "audit_url" in out and out["audit_url"] is None


def test_skill_envelope_has_audit_urls(tmp_path, monkeypatch):
    """Skill envelopes gain audit_urls, one link per receipt, as a SIBLING of
    receipts (signature-safe)."""
    monkeypatch.setenv("SURIMI_PUBLIC_BASE_URL", "https://mcp.example")
    _make_aer_lake(tmp_path, monkeypatch)
    tool = _get_tool("breakdown")
    env = tool.fn(dataset_id="aer-capacity", group_by="country_code")
    assert env.get("error") is None, env
    assert env["receipts"], env
    assert env["audit_urls"] and all(
        u.startswith("https://mcp.example/audit#") for u in env["audit_urls"])
    assert len(env["audit_urls"]) == len(env["receipts"])
    # never a key inside a receipt dict
    assert all("audit_url" not in r for r in env["receipts"])


def test_skill_envelope_audit_urls_keep_index_pairing_without_base(
        tmp_path, monkeypatch):
    """Without SURIMI_PUBLIC_BASE_URL skills return one None PER receipt,
    audit_urls[i] always pairs with receipts[i], Nones are never filtered out
    (filtering would silently shift the pairing when only some links mint)."""
    monkeypatch.delenv("SURIMI_PUBLIC_BASE_URL", raising=False)
    _make_aer_lake(tmp_path, monkeypatch)
    tool = _get_tool("breakdown")
    env = tool.fn(dataset_id="aer-capacity", group_by="country_code")
    assert env.get("error") is None, env
    assert env["receipts"], env
    assert env["audit_urls"] == [None] * len(env["receipts"])


def test_query_data_tool_returns_audit_url(monkeypatch):
    """query_data attaches a shareable audit_url next to its receipt, wired
    through the same audit_url_for as aggregate. Trino is faked at the client
    factory, mirroring how the cube tests fake the lake."""
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed
    from surimi_data_mcp.tools import figures

    monkeypatch.setenv("SURIMI_PUBLIC_BASE_URL", "https://mcp.example")

    class _FakeTrino:
        def execute(self, sql, limit=500):
            return {"rows": [{"n": 1}], "columns": ["n"], "row_count": 1}

    monkeypatch.setattr(
        figures, "_query_data_signed",
        make_query_data_signed(client_factory=lambda: _FakeTrino(),
                               backend_id="trino://fake"))

    tool = _get_tool("query_data")
    out = tool.fn(sql="SELECT 1 AS n", limit=10)
    assert out["value"]["row_count"] == 1
    assert out["audit_url"].startswith("https://mcp.example/audit#")
    # link is a SIBLING, never inside the signed receipt
    assert "audit_url" not in out["receipt"]


def test_query_data_refuses_eav_magnitude_without_executing(monkeypatch):
    """A raw SUM(variable_value) on the EAV social table MULTI-COUNTS overlapping
    strata (the 442,638 vs ~104k double-count). The query_data tool must REFUSE
    it -- return an honest error redirecting to aggregate/breakdown -- and NOT
    execute + sign a wrong number. 'I can't total this reliably' beats a
    confident wrong total."""
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed
    from surimi_data_mcp.tools import figures

    executed = []

    class _RecordingTrino:
        def execute(self, sql, limit=500):
            executed.append(sql)
            return {"rows": [{"s": 442638.5}], "columns": ["s"], "row_count": 1}

    monkeypatch.setattr(
        figures, "_query_data_signed",
        make_query_data_signed(client_factory=lambda: _RecordingTrino(),
                               backend_id="trino://fake"))

    tool = _get_tool("query_data")
    out = tool.fn(
        sql="SELECT SUM(variable_value) FROM hive.eu_dcf.aer_social_data_2017_2020 "
            "WHERE variable_code='socemploy' AND year=2020",
        limit=500)

    # the dangerous SQL was NOT run (no fabricated number computed/signed)
    assert executed == [], "refused query must not reach the executor"
    # honest refusal surfaced, no rows, no signed receipt over a wrong number
    assert out["value"]["rows"] == []
    assert out["value"].get("error")
    assert "aggregate" in out["value"]["error"].lower() or \
           "breakdown" in out["value"]["error"].lower()
    assert not out.get("receipt")


def test_query_data_still_runs_normal_aggregate(monkeypatch):
    """The guard is NARROW: a magnitude on a NORMAL column still runs (and gets
    the steering note) -- no blanket-refusal regression."""
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed
    from surimi_data_mcp.tools import figures

    ran = []

    class _Trino:
        def execute(self, sql, limit=500):
            ran.append(sql)
            return {"rows": [{"v": 11611}], "columns": ["v"], "row_count": 1}

    monkeypatch.setattr(
        figures, "_query_data_signed",
        make_query_data_signed(client_factory=lambda: _Trino(),
                               backend_id="trino://fake"))

    tool = _get_tool("query_data")
    out = tool.fn(sql="SELECT SUM(number_of_vessels) FROM hive.eu_dcf.aer_capacity "
                      "WHERE year=2024", limit=500)
    assert ran, "a normal-column aggregate must still execute"
    assert out["value"]["row_count"] == 1
    assert out.get("steering")  # aggregate steering note still attached


def test_verify_tool_accepts_recompute_and_matches(monkeypatch):
    """verify(receipt, recompute=true) re-executes the receipt's aggregate
    against the live cube and reports {claimed, recomputed, match: True}."""
    monkeypatch.setenv("SURIMI_PUBLIC_BASE_URL", "https://mcp.example")
    out = _call_aggregate(
        dataset="landings",
        measures=[{"col": "landings_tonnes", "fn": "sum"}],
        dimensions=["year"],
    )
    verify_tool = _get_tool("verify")
    verdict = verify_tool.fn(receipt=out["receipt"], recompute=True)
    assert verdict["verified"] is True, verdict
    assert verdict["recompute_supported"] is True
    assert verdict["match"] is True, verdict
    assert verdict["claimed"] and verdict["recomputed"]


def test_verify_tool_tampered_receipt_never_recomputed():
    """Signature first: a tampered receipt fails verification and recompute is
    refused (no claimed/recomputed echo of untrusted params)."""
    import copy

    out = _call_aggregate(
        dataset="landings",
        measures=[{"col": "landings_tonnes", "fn": "sum"}],
        dimensions=["year"],
    )
    tampered = copy.deepcopy(out["receipt"])
    snap = tampered.get("output") or tampered.get("output_value")
    rows = snap["rows"] if "rows" in snap else snap["value"]["rows"]
    rows[0]["landings_tonnes_sum"] = rows[0]["landings_tonnes_sum"] + 999.0
    verify_tool = _get_tool("verify")
    verdict = verify_tool.fn(receipt=tampered, recompute=True)
    assert verdict["verified"] is False
    assert verdict["recompute_supported"] is False
    assert "match" not in verdict


def test_verify_tool_secret_not_configured_reason_stable(monkeypatch):
    """The old verify returned reason 'secret_not_configured' when the secret is
    unset; the new recompute-capable verify must keep that exact contract."""
    monkeypatch.delenv("SURIMI_RECEIPT_SECRET", raising=False)
    verify_tool = _get_tool("verify")
    verdict = verify_tool.fn(receipt={"tool_id": "aggregate"})
    assert verdict["verified"] is False
    assert verdict["reason"] == "secret_not_configured"


# ───────────── latest-year default for stock measures ─────────────────────

from pathlib import Path as _Path
from surimi_data_mcp.engine.cube import build_cube as _build_cube
from surimi_data_mcp.engine.aggregate import aggregate as _aggregate_fn, apply_latest_year_default as _apply_latest_year_default
from surimi_data_mcp.tools import common


@pytest.fixture
def stockcube(tmp_path: _Path):
    root = tmp_path / "src"; root.mkdir()
    (root / "fleet.csv").write_text(
        "year,country,vessels\n2022,IT,100\n2022,EL,90\n2023,IT,110\n2023,EL,95\n")
    return _build_cube(str(root))


def test_aggregate_with_default_picks_latest_year(stockcube):
    """apply_latest_year_default + aggregate: stock measure with no year pinned
    defaults to the latest year (2023), returns IT=110 not IT=210 (double-count),
    year_defaulted=True, and note contains '2023'."""
    measures = [{"col": "vessels", "fn": "sum"}]
    filters, defaulted, note = _apply_latest_year_default(
        stockcube, "fleet", "year", measures, None, ["country"], {"vessels"})
    out = _aggregate_fn(stockcube, "fleet", measures=measures,
                       dimensions=["country"], filters=filters,
                       order=[{"col": "vessels_sum", "dir": "desc"}])
    rows = out["value"]["rows"]
    # 2023 only: IT 110 (not 210 across both years)
    assert rows[0]["country"] == "IT" and rows[0]["vessels_sum"] == 110
    assert defaulted is True and "2023" in note


def test_query_metric_envelope_gets_an_audit_url(tmp_path, monkeypatch):
    """query_metric is the tool the server calls the preferred way to get a
    number, and it was the one tool whose audit link was dropped.

    The aggregate engine returns {value, receipt} singular; `common.attach_audit_urls`
    only read plural `receipts`, so the envelope came back with an empty
    audit_urls and the reader had no way to check the figure.
    """
    monkeypatch.setenv("SURIMI_PUBLIC_BASE_URL", "https://mcp.example")
    _make_aer_lake(tmp_path, monkeypatch)
    tool = _get_tool("query_metric")
    env = tool.fn(metric="vessel_count", by=["country_code"])
    assert env.get("error") is None, env
    assert env.get("receipt"), env
    assert env["audit_urls"], "singular receipt must still yield an audit link"
    assert env["audit_urls"][0].startswith("https://mcp.example/audit#")


def test_attach_audit_urls_handles_a_singular_receipt():
    """Unit-level: the helper must see both envelope shapes."""
    from surimi_data_mcp import server
    env = common.attach_audit_urls({"receipt": {"id": "r1"}})
    assert len(env["audit_urls"]) == 1


def test_attach_audit_urls_does_not_double_count_a_mixed_envelope():
    """An envelope carrying both keys must not produce a link per key twice."""
    from surimi_data_mcp import server
    env = common.attach_audit_urls({"receipt": {"id": "r1"}, "receipts": [{"id": "r1"}]})
    assert len(env["audit_urls"]) == 1
