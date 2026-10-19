# tests/test_metrics.py
import os
from pathlib import Path
import pytest
from surimi_data_mcp.engine.cube import build_cube
from surimi_data_mcp.datasets import metrics
from surimi_data_mcp.datasets.metrics import Metric, MetricError, validate_registry, resolve, run_query_metric, catalog_prompt, REGISTRY
from surimi_data_mcp.tools import discovery

_SECRET = b"test-metrics-secret-do-not-use-in-prod"

@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    """The aggregate engine signs receipts from $SURIMI_RECEIPT_SECRET."""
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", _SECRET.decode())

@pytest.fixture
def minicube(tmp_path: Path):
    root = tmp_path / "src"; root.mkdir()
    (root / "fleet.csv").write_text(
        "year,country,vessels,revenue,margin\n"
        "2022,IT,100,10.0,0.2\n2023,IT,110,11.0,0.25\n")
    # The FDI table's country column holds full names ("Italy"), not codes.
    (root / "named_fleet.csv").write_text(
        "year,country,vessels\n"
        "2023,Italy,110\n2023,Spain,220\n2023,France,330\n")
    return build_cube(str(root))

def test_metric_is_frozen():
    m = Metric(name="x", label="X", dataset="fleet", measure="vessels",
               agg="sum", additivity="stock", unit="vessels", dimensions=("country",))
    with pytest.raises(Exception):
        m.name = "y"  # frozen

def test_validate_rejects_ratio_sum(minicube):
    bad = (Metric(name="m", label="M", dataset="fleet", measure="margin",
                  agg="sum", additivity="ratio", unit="%", dimensions=("country",)),)
    with pytest.raises(MetricError):
        validate_registry(bad, minicube)

def test_validate_rejects_unknown_column(minicube):
    bad = (Metric(name="m", label="M", dataset="fleet", measure="nope",
                  agg="sum", additivity="flow", unit="x", dimensions=("country",)),)
    with pytest.raises(MetricError):
        validate_registry(bad, minicube)

def test_validate_accepts_good_registry(minicube):
    good = (Metric(name="vessel_count", label="Vessels", dataset="fleet",
                   measure="vessels", agg="sum", additivity="stock",
                   unit="vessels", dimensions=("country",)),)
    validate_registry(good, minicube)  # no raise

def test_validate_rejects_bad_additivity(minicube):
    bad = (Metric(name="m", label="M", dataset="fleet", measure="vessels",
                  agg="sum", additivity="nonsense", unit="x", dimensions=("country",)),)
    with pytest.raises(MetricError):
        validate_registry(bad, minicube)

def test_validate_rejects_duplicate_name(minicube):
    dup = (
        Metric(name="m", label="M", dataset="fleet", measure="vessels",
               agg="sum", additivity="stock", unit="vessels", dimensions=("country",)),
        Metric(name="m", label="M2", dataset="fleet", measure="vessels",
               agg="sum", additivity="stock", unit="vessels", dimensions=("country",)),
    )
    with pytest.raises(MetricError):
        validate_registry(dup, minicube)


# ─────── resolve + run_query_metric ───────

# A registry pointing at the synthetic 'fleet' cube (DI lets us test hermetically).
TEST_REG = (
    Metric("vessel_count", "Vessels", "fleet", "vessels", "sum", "stock",
           "vessels", ("country",), origin="observed"),
    Metric("fleet_revenue", "Revenue", "fleet", "revenue", "sum", "flow",
           "EUR", ("country",), origin="observed"),
)

@pytest.fixture
def fleetcube(tmp_path: Path):
    root = tmp_path / "src"; root.mkdir()
    (root / "fleet.csv").write_text(
        "year,country,vessels,revenue\n"
        "2022,IT,100,10.0\n2022,EL,90,9.0\n2023,IT,110,11.0\n2023,EL,95,9.5\n")
    return build_cube(str(root))

def test_resolve_unknown_raises():
    with pytest.raises(MetricError):
        resolve("nope", registry=TEST_REG)

def test_stock_metric_defaults_latest_year(fleetcube):
    out = run_query_metric(fleetcube, "vessel_count", by=["country"],
                           order=[{"col": "vessels_sum", "dir": "desc"}],
                           registry=TEST_REG)
    v = out["value"]
    assert v["year_defaulted"] is True
    assert v["rows"][0]["country"] == "IT" and v["rows"][0]["vessels_sum"] == 110  # 2023, not 210
    assert v.get("unit") == "vessels" and v.get("metric") == "vessel_count"
    assert "receipt" in out

def test_flow_metric_sums_all_years(fleetcube):
    out = run_query_metric(fleetcube, "fleet_revenue", by=["country"], registry=TEST_REG)
    v = out["value"]
    assert not v.get("year_defaulted")
    it = [r for r in v["rows"] if r["country"] == "IT"][0]
    assert it["revenue_sum"] == pytest.approx(21.0)  # 10 + 11 across both years

def test_year_pin_overrides_default(fleetcube):
    out = run_query_metric(fleetcube, "vessel_count", by=["country"], year=2022,
                           registry=TEST_REG)
    it = [r for r in out["value"]["rows"] if r["country"] == "IT"][0]
    assert it["vessels_sum"] == 100 and not out["value"].get("year_defaulted")

def test_by_must_be_allowed_dimension(fleetcube):
    with pytest.raises(MetricError):
        run_query_metric(fleetcube, "vessel_count", by=["not_a_dim"], registry=TEST_REG)

def test_year_default_note_has_no_ok_prefix(fleetcube):
    out = run_query_metric(fleetcube, "vessel_count", by=["country"], registry=TEST_REG)
    notes = out["value"].get("notes", "")
    assert not notes.lower().startswith("ok")
    assert "latest year" in notes.lower()


@pytest.mark.skipif(not os.environ.get("SURIMI_CUBE_DB"),
                    reason="needs the real cube (SURIMI_CUBE_DB)")
def test_registry_resolves_against_real_cube():
    from surimi_data_mcp.engine import cube as cube_module
    from surimi_data_mcp.datasets.metrics import REGISTRY, validate_registry
    validate_registry(REGISTRY, cube_module.get_cube())  # must not raise


# ─────── catalog_prompt ───────

def test_catalog_prompt_lists_every_metric_with_unit_and_additivity():
    txt = catalog_prompt()
    for m in REGISTRY:
        assert m.name in txt
        assert m.unit in txt
    assert "query_metric" in txt
    # stock metrics advertise the latest-year default; flow metrics advertise summing
    assert "latest year" in txt.lower()


def test_cube_dataset_resolves_sanitized_catalog_table(tmp_path):
    """REGRESSION (0.7.7 live bug): a metric's catalog dataset id ('aer-capacity')
    maps to table 'hive.eu_dcf.aer_capacity', but the cube keys it by the SANITIZED
    stem 'hive_eu_dcf_aer_capacity'. _cube_dataset must try the sanitized name, or
    query_metric errors 'dataset not in cube' on the real cube (synthetic fixtures
    named 'fleet' hid this)."""
    from surimi_data_mcp.datasets.metrics import _cube_dataset, Metric
    from surimi_data_mcp.engine.cube import build_cube
    root = tmp_path / "lake"; root.mkdir()
    # stem 'hive.eu_dcf.aer_capacity' -> cube dataset 'hive_eu_dcf_aer_capacity'
    (root / "hive.eu_dcf.aer_capacity.csv").write_text(
        "country_name,year,number_of_vessels\nItaly,2024,11611\n", encoding="utf-8")
    cube = build_cube(str(root))
    m = Metric("vessel_count", "Vessels", "aer-capacity", "number_of_vessels",
               "sum", "stock", "vessels", ("country_name",))
    assert _cube_dataset(m, cube) == "hive_eu_dcf_aer_capacity"


# ── list_metrics discovery tool ──────────────────────────────────────────────

def test_list_metrics_exposes_registry_semantics():
    """The model must be able to discover metric names AND their additivity
    before calling query_metric, otherwise it guesses at time semantics."""
    from surimi_data_mcp import server

    out = discovery.list_metrics.fn() if hasattr(discovery.list_metrics, "fn") else discovery.list_metrics()
    names = {m["name"] for m in out["metrics"]}

    from surimi_data_mcp.datasets import metrics as metrics_module
    assert names == {m.name for m in metrics_module.REGISTRY}
    for m in out["metrics"]:
        assert set(m) >= {"name", "label", "unit", "additivity", "dimensions"}
        assert m["additivity"] in {"stock", "flow", "ratio"}
    assert out["prompt"]


# --- flow metrics disclose the window they summed --------------------------
# Regression cover for PROBE-RESULTS-2026-09-10.md, Q-AS3: a flow metric with no
# year pinned sums every year in the table, and the answer stated the cumulative
# figure as if it were one year's.

_FLOW = (Metric(name="revenue", label="Revenue", dataset="fleet", measure="revenue",
                agg="sum", additivity="flow", unit="EUR",
                dimensions=("country", "year"), origin="observed"),)
_STOCK = (Metric(name="vessels", label="Vessels", dataset="fleet", measure="vessels",
                 agg="sum", additivity="stock", unit="vessels", dimensions=("country",),
                 origin="observed"),)


def test_flow_metric_reports_the_years_it_summed(minicube):
    out = run_query_metric(minicube, "revenue", by=["country"], registry=_FLOW)
    v = out["value"]
    assert v["years_covered"] == "2022-2023"
    assert "2022-2023" in v["notes"]


def test_flow_metric_says_nothing_when_the_year_is_pinned(minicube):
    out = run_query_metric(minicube, "revenue", by=["country"], year=2023, registry=_FLOW)
    assert "years_covered" not in out["value"]


def test_flow_metric_says_nothing_when_grouped_by_year(minicube):
    out = run_query_metric(minicube, "revenue", by=["year"], registry=_FLOW)
    assert "years_covered" not in out["value"]


def test_stock_metric_keeps_its_own_latest_year_note(minicube):
    # The semi-additive guard already pins the latest year for a stock; the new
    # flow disclosure must not fire on top of it.
    out = run_query_metric(minicube, "vessels", by=["country"], registry=_STOCK)
    v = out["value"]
    assert "years_covered" not in v
    assert v.get("year_defaulted") is True


# ---------- a filter value must reach the column it names ----------

def _named_registry():
    return (Metric(name="fdi_vessels", label="Fleet vessels (FDI)",
                   dataset="named_fleet", measure="vessels", agg="sum",
                   additivity="stock", unit="vessels", dimensions=("country",),
                   origin="observed"),)


def test_metric_filter_matches_a_country_name(minicube):
    """The baseline: a name against a name column already works."""
    out = run_query_metric(minicube, "fdi_vessels", by=["country"],
                           where=[{"col": "country", "op": "=", "value": "Spain"}],
                           registry=_named_registry())
    rows = out["value"]["rows"]
    assert len(rows) == 1 and rows[0]["vessels_sum"] == 220


def test_metric_filter_accepts_an_iso_code_for_a_name_column(minicube):
    """The FDI country column holds "Spain"; a model or user filtering "ESP"
    got an empty result and no indication why.

    An empty result is read as "no data for Spain", which is a claim about the
    data rather than about the filter.
    """
    out = run_query_metric(minicube, "fdi_vessels", by=["country"],
                           where=[{"col": "country", "op": "=", "value": "ESP"}],
                           registry=_named_registry())
    rows = out["value"]["rows"]
    assert len(rows) == 1, out["value"]
    assert rows[0]["vessels_sum"] == 220


# ---------- Q-E11-3: origin must be declared, and it must reach the envelope --

def test_query_metric_surfaces_the_metric_origin(fleetcube):
    out = run_query_metric(fleetcube, "vessel_count", by=["country"], registry=TEST_REG)
    assert out["value"]["origin"] == "observed"


def test_query_metric_raises_when_origin_is_not_declared(fleetcube):
    """ANSWER-CONTRACT.md: origin must never be silently omitted. A metric
    that forgot to declare one must fail loudly, not return an envelope
    missing the field."""
    no_origin = (Metric("vessel_count", "Vessels", "fleet", "vessels", "sum",
                        "stock", "vessels", ("country",)),)  # origin left at default ""
    with pytest.raises(MetricError):
        run_query_metric(fleetcube, "vessel_count", by=["country"], registry=no_origin)


def test_every_registry_metric_declares_an_origin():
    """The real, shipped REGISTRY -- not a test fixture -- must never regress
    into a metric with no origin declared."""
    missing = [m.name for m in REGISTRY if not m.origin]
    assert not missing, f"metrics with no origin: {missing}"


def test_metric_filter_says_when_a_value_matched_nothing(minicube):
    """A filter value present in no row must be reported, not returned empty."""
    out = run_query_metric(minicube, "fdi_vessels", by=["country"],
                           where=[{"col": "country", "op": "=", "value": "Atlantis"}],
                           registry=_named_registry())
    assert out["value"]["rows"] == []
    assert "Atlantis" in str(out["value"].get("unmatched_filter_values"))
