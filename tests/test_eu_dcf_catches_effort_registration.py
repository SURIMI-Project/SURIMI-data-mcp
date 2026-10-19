"""Q-E1-8a: "What happens to trawling revenues in Spain if I close fishing from
January to March?" SURIMI holds the historical baseline (EU_DCF_Catches.csv,
1.9 GB / 1.29M Spanish rows, plus a companion quarterly effort file), but
neither was registered in the catalogue, so no served tool could reach it.

Both are registered as `eu-dcf-catches` and `eu-dcf-effort` in `datasets/datasets.yaml`,
cube-only (NOT added to `loader/load_csv.py`'s CSV_TO_TABLE / Postgres subset;
tests/test_catalog_additions.py::test_the_fdi_catches_file_is_not_loaded_into_postgres
still governs that, unchanged). The measure columns carry the literal string
"C" wherever a row is confidentiality-suppressed; `engine/aggregate.py`'s
TRY_CAST-on-VARCHAR-magnitude guard already nulls that out correctly (verified
against the real lake in benchmark/experts/raw-grounding/WAVE-3-real-answers.md),
so registration alone is enough to make `aggregate`/`query_metric` reach it.
"""
from __future__ import annotations

import csv as _csv
from pathlib import Path

import pytest

from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.tools import common

SECRET = b"test-eu-dcf-catches-secret-do-not-use"

# The real EU_DCF_Catches.csv header (see references/SURIMI-DATA-LAKE/1_Fisheries/
# 1.3_EU DCF Catches/README.txt), trimmed to the columns exercised below.
CATCHES_HEADER = [
    "country", "year", "quarter", "fishing_tech", "spe_code", "supra_region",
    "total_value_of_landings", "confidential",
]

# The real "EU DCF Effort by country.csv" header (dotted/mixed-case upstream),
# trimmed the same way.
EFFORT_HEADER = [
    "Country", "Year", "Quarter", "fishing_tech", "Supra.region",
    "Total.days.at.sea", "Confidential",
]


def _w(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


# ── catalog registration ─────────────────────────────────────────────────────

def test_eu_dcf_catches_is_registered():
    ds = catalog.find_dataset("eu-dcf-catches")
    assert ds is not None
    assert ds["table"] == "hive.eu_dcf.eu_dcf_catches"
    assert ds["domain"] == "eu_dcf"
    names = {c["name"] for c in ds["columns"]}
    assert {"country", "year", "quarter", "fishing_tech", "spe_code",
            "supra_region", "total_value_of_landings"} <= names


def test_eu_dcf_effort_is_registered():
    ds = catalog.find_dataset("eu-dcf-effort")
    assert ds is not None
    assert ds["domain"] == "eu_dcf"
    # table's last segment must equal the sanitized stem of the real filename
    # "EU DCF Effort by country.csv", or aggregate/query_metric cannot resolve
    # the catalog id against the real cube (see metrics._cube_dataset).
    assert ds["table"].split(".")[-1] == "eu_dcf_effort_by_country"


def test_eu_dcf_catches_table_matches_the_real_file_stem():
    """table.split('.')[-1] must equal _sanitize_name("EU_DCF_Catches"), or the
    catalog id never resolves against the real (raw-lake) cube."""
    from surimi_data_mcp.engine.cube import _sanitize_name
    ds = catalog.find_dataset("eu-dcf-catches")
    assert ds["table"].split(".")[-1] == _sanitize_name("EU_DCF_Catches")


def test_eu_dcf_catches_value_column_declares_flow_additivity():
    ds = catalog.find_dataset("eu-dcf-catches")
    col = next(c for c in ds["columns"] if c["name"] == "total_value_of_landings")
    assert col["additivity"] == "flow"


def test_eu_dcf_catches_entry_carries_both_experts_columns_and_warnings():
    """The entry carries the full 29-column export (gear, species and discards, which
    Q-E2-2 needs), the additivity tag on the three measure columns, the Spanish-DTS
    confidentiality statistic (Q-E1-8a) and the "no climate projection" caveat."""
    ds = catalog.find_dataset("eu-dcf-catches")
    names = {c["name"] for c in ds["columns"]}
    # Columns for Q-E2-2: discard and gear composition.
    for col in ("gear_type", "tot_discards_tonnes", "confidential", "english_name",
                "scientific_name", "vessel_length", "target_assemblage"):
        assert col in names, f"E2's column {col!r} was dropped in the merge"
    # Every measure column keeps its additivity tag.
    for col in ("total_live_weight_landed", "total_value_of_landings", "tot_discards_tonnes"):
        c = next(c for c in ds["columns"] if c["name"] == col)
        assert c.get("additivity") == "flow", f"{col} lost its additivity tag"
    warn = ds["not_a_substitute_for"]
    assert "330,212" in warn and "501,244" in warn  # the Spanish-DTS statistic
    assert "climate" in warn or "scenario" in warn  # the no-climate-projection caveat


def test_confidentiality_sentinel_warning_rides_on_the_result():
    """BRIEF rule: a capability declaration is not a guard -- the warning must
    ride on the tool RESULT (catalog.substitute_warning is wired into
    aggregate/query_metric/query_data's output), not just be discoverable via
    list_datasets."""
    warn = catalog.substitute_warning("eu-dcf-catches")
    assert warn, "eu-dcf-catches must carry a not_a_substitute_for warning"
    assert "C" in warn


def test_neither_new_dataset_was_added_to_postgres():
    """Cube-only: the 1.9 GB catches file (and its effort companion) must not
    be added to the Postgres analytic subset -- that decision (confidentiality
    flag semantics) is what test_catalog_additions.py's
    test_the_fdi_catches_file_is_not_loaded_into_postgres protects."""
    from surimi_data_mcp.loader.load_csv import CSV_TO_TABLE
    values = " ".join(CSV_TO_TABLE.values()).lower()
    assert "eu_dcf_catches" not in values
    assert "eu_dcf_effort" not in values


# ── end-to-end: the deployed `aggregate` tool reaches the real shape of data ──

@pytest.fixture
def lake_dir(tmp_path: Path) -> Path:
    """A cube-buildable dir with files named EXACTLY like the real lake, so the
    catalog id resolves the same way it would against the real raw-lake cube."""
    d = tmp_path / "lake"
    d.mkdir()
    # Spain DTS: 3 non-confidential rows (Q1, Q2, Q3) + 1 confidential ("C") Q4
    # row that must be EXCLUDED from the sum, not treated as zero or crash the
    # aggregate. Plus one Belgium row that must not leak into a Spain filter.
    _w(d / "EU_DCF_Catches.csv", CATCHES_HEADER, [
        ["Spain", 2023, 1, "DTS", "HKE", "MED", "100.0", "N"],
        ["Spain", 2023, 2, "DTS", "HKE", "MED", "200.0", "N"],
        ["Spain", 2023, 3, "DTS", "HKE", "MED", "300.0", "N"],
        ["Spain", 2023, 4, "DTS", "HKE", "MED", "C", "A"],
        ["Belgium", 2023, 1, "DTS", "HKE", "NAO", "9999.0", "N"],
    ])
    _w(d / "EU DCF Effort by country.csv", EFFORT_HEADER, [
        ["Spain", 2023, 1, "DTS", "MED", "50.0", "N"],
        ["Spain", 2023, 2, "DTS", "MED", "60.0", "N"],
    ])
    return d


@pytest.fixture(autouse=True)
def _wire_env(monkeypatch, lake_dir: Path):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", SECRET.decode())
    monkeypatch.setenv("SURIMI_DATA_DIR", str(lake_dir))
    monkeypatch.setenv("SURIMI_CUBE_DB", ":memory:")
    from surimi_data_mcp.engine import cube
    monkeypatch.setattr(cube, "_CUBE", None, raising=False)
    yield
    monkeypatch.setattr(cube, "_CUBE", None, raising=False)


def test_resolve_cube_dataset_bridges_a_raw_lake_stem_shorter_than_the_table():
    """The catalog table is "hive.eu_dcf.eu_dcf_effort_by_country", but the
    real raw-lake file sanitizes to the bare stem "eu_dcf_effort_by_country"
    (no "hive_eu_dcf_" prefix) -- unlike the "hive_eu_dcf_X"-named exports the
    existing bridge candidates were built against. _resolve_cube_dataset must
    also try table.split('.')[-1] (mirrors metrics._cube_dataset), or every
    raw-lake-shaped registration with a shorter real stem stays unreachable."""
    from surimi_data_mcp import server

    class _FakeCube:
        def datasets(self):
            return ["eu_dcf_effort_by_country"]

    assert common.resolve_cube_dataset("eu-dcf-effort", _FakeCube()) == "eu_dcf_effort_by_country"


def _aggregate_tool():
    import asyncio

    from surimi_data_mcp import server
    tools = asyncio.run(server.mcp.list_tools())
    for t in tools:
        if t.name == "aggregate":
            return t
    raise AssertionError("aggregate tool not registered")


def test_aggregate_tool_reaches_eu_dcf_catches_by_catalog_id():
    """The exact tool call an LLM would make: aggregate(dataset='eu-dcf-catches',
    measures=[sum(total_value_of_landings)], by=[quarter], where country=Spain).
    Must resolve the catalog id against the real-shaped cube. The confidential
    "C" row must be excluded from the sum; it must not error and must not be
    treated as a zero."""
    tool = _aggregate_tool()
    out = tool.fn(
        dataset="eu-dcf-catches",
        measures=[{"col": "total_value_of_landings", "fn": "sum"}],
        dimensions=["quarter"],
        filters=[{"col": "country", "op": "=", "value": "Spain"}],
    )
    assert out["value"] is not None, out
    rows = {r["quarter"]: r["total_value_of_landings_sum"] for r in out["value"]["rows"]}
    assert rows[1] == pytest.approx(100.0)
    assert rows[2] == pytest.approx(200.0)
    assert rows[3] == pytest.approx(300.0)
    # Q4 is entirely confidential ("C"): its SUM is NULL, never a zero.
    assert rows[4] is None


def test_aggregate_tool_reaches_eu_dcf_effort_by_catalog_id():
    tool = _aggregate_tool()
    out = tool.fn(
        dataset="eu-dcf-effort",
        measures=[{"col": "total_days_at_sea", "fn": "sum"}],
        dimensions=["quarter"],
        filters=[{"col": "country", "op": "=", "value": "Spain"}],
    )
    assert out["value"] is not None, out
    rows = {r["quarter"]: r["total_days_at_sea_sum"] for r in out["value"]["rows"]}
    assert rows[1] == pytest.approx(50.0)
    assert rows[2] == pytest.approx(60.0)
