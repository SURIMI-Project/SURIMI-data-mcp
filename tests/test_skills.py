"""Server-executed skill executors: pure, run against a fixture cube."""
import csv as _csv
from pathlib import Path

import pytest

from surimi_data_mcp.engine.cube import build_cube
from surimi_data_mcp.provenance.receipts import verify_receipt

SECRET = b"test-skills-secret-do-not-use-in-prod"


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", SECRET.decode())


# A biomass-style series: 2 species x 3 years, monotone so direction is knowable.
BIOMASS_ROWS = [
    ["year", "species_code", "biomass_kt"],
    ["2012", "PIL", "80.0"],
    ["2013", "PIL", "78.0"],
    ["2014", "PIL", "76.0"],
    ["2012", "HKE", "10.0"],
    ["2013", "HKE", "12.0"],
    ["2014", "HKE", "15.0"],
]

COUNTRY_ROWS = [
    ["year", "country_code", "vessels"],
    ["2020", "ESP", "100"],
    ["2020", "ITA", "60"],
    ["2020", "FRA", "40"],
]

# Both a code column and a name column, as the real fleet tables have. Typing
# "Spain" matched nothing and the user was told there were no values, which
# reads as missing data rather than an unrecognised input.
NAMED_COUNTRY_ROWS = [
    ["year", "country_code", "country_name", "landings"],
    ["2020", "ESP", "Spain", "500"],
    ["2020", "ITA", "Italy", "300"],
    ["2020", "FRA", "France", "200"],
]

# A stock (vessel count) over three years. Summing every year gives 600 for a
# fleet that never exceeded 220: the shape of the 332,615-vessel answer an
# expert flagged against a truth of 11,996.
FLEET_ROWS = [
    ["year", "country_code", "vessels"],
    ["2018", "ESP", "90"], ["2018", "ITA", "50"], ["2018", "FRA", "30"],
    ["2019", "ESP", "95"], ["2019", "ITA", "55"], ["2019", "FRA", "35"],
    ["2020", "ESP", "100"], ["2020", "ITA", "60"], ["2020", "FRA", "40"],
]


def _write_csv(path: Path, rows):
    with open(path, "w", newline="") as f:
        _csv.writer(f).writerows(rows)


@pytest.fixture
def cube(tmp_path: Path):
    root = tmp_path / "lake"
    root.mkdir()
    _write_csv(root / "biomass.csv", BIOMASS_ROWS)
    _write_csv(root / "country.csv", COUNTRY_ROWS)
    _write_csv(root / "fleet.csv", FLEET_ROWS)
    _write_csv(root / "named.csv", NAMED_COUNTRY_ROWS)
    _write_csv(root / "gender.csv", [
        ["sex", "n"],
        ["Male", "1"], ["male", "1"], ["MALE", "1"],
        ["Female", "1"], ["female", "1"], ["unknown", "1"],
    ])
    _write_csv(root / "eav.csv", [
        ["country", "variable_code", "value"],
        ["ESP", "employment_fte", "1000"], ["FRA", "employment_fte", "800"],
        ["ESP", "landings_tonnes", "5000"], ["FRA", "landings_tonnes", "4000"],
    ])
    return build_cube(str(root))


def test_period_key_orders_numeric_then_text():
    from surimi_data_mcp.engine.skills import _period_key
    assert _period_key("2012") < _period_key("2013")
    assert _period_key(2012) < _period_key("2013")


def test_trend_finding_detects_direction_and_range():
    from surimi_data_mcp.engine.skills import trend_finding
    rows = [
        {"year": "2012", "species_code": "PIL", "biomass_kt_sum": 80.0},
        {"year": "2014", "species_code": "PIL", "biomass_kt_sum": 76.0},
        {"year": "2012", "species_code": "HKE", "biomass_kt_sum": 10.0},
        {"year": "2014", "species_code": "HKE", "biomass_kt_sum": 15.0},
    ]
    finding, summary = trend_finding(rows, "year", "species_code", "biomass_kt_sum")
    assert summary["PIL"]["direction"] == "decreasing"
    assert summary["HKE"]["direction"] == "increasing"
    assert summary["PIL"]["min"] == 76.0 and summary["PIL"]["max"] == 80.0
    assert "PIL" in finding and "HKE" in finding


def test_chart_from_rows_grouped_is_non_degenerate():
    from surimi_data_mcp.engine.skills import chart_from_rows
    rows = [
        {"year": "2012", "species_code": "PIL", "biomass_kt_sum": 80.0},
        {"year": "2013", "species_code": "PIL", "biomass_kt_sum": 78.0},
        {"year": "2012", "species_code": "HKE", "biomass_kt_sum": 10.0},
        {"year": "2013", "species_code": "HKE", "biomass_kt_sum": 12.0},
    ]
    chart = chart_from_rows(rows, "year", "species_code", "biomass_kt_sum", kind="line")
    assert chart["kind"] == "line"
    assert chart["labels"] == ["2012", "2013"]
    names = sorted(s["name"] for s in chart["series"])
    assert names == ["HKE", "PIL"]
    # one value per label, aligned
    for s in chart["series"]:
        assert len(s["values"]) == len(chart["labels"])
    # labels are NOT all identical (the degenerate-chart bug)
    assert len(set(chart["labels"])) == len(chart["labels"])


def test_run_analyze_trend_executes_and_signs(cube):
    from surimi_data_mcp.engine.skills import run_analyze_trend
    env = run_analyze_trend(cube, dataset_name="biomass", time_column="year",
                            metric="biomass_kt", group_by="species_code")
    assert env["skill"] == "analyze_trend"
    # real engine numbers, not the model's
    pil = env["data"]["summary"]["PIL"]
    assert pil["direction"] == "decreasing"
    assert pil["first"] == 80.0 and pil["last"] == 76.0
    # chart is grouped + non-degenerate
    assert env["chart"]["kind"] == "line"
    assert len(set(env["chart"]["labels"])) == len(env["chart"]["labels"])
    # exactly one receipt, and it VERIFIES
    assert len(env["receipts"]) == 1
    assert verify_receipt(env["receipts"][0], secret=SECRET)["verified"] is True
    # notes is a CLEAN list (engine "ok" -> [], then the code-label note);
    # NOT a char-split of the engine's "ok" string.
    assert env["notes"] == ["group codes shown verbatim; no common-name mapping in the dataset"]


def test_run_analyze_trend_no_group(cube):
    from surimi_data_mcp.engine.skills import run_analyze_trend
    env = run_analyze_trend(cube, dataset_name="biomass", time_column="year",
                            metric="biomass_kt")
    assert "values" in env["chart"]
    assert env["provenance"]["coverage"] == "2012-2014"


def test_run_compare_countries_ranks_and_signs(cube):
    from surimi_data_mcp.engine.skills import run_compare_countries
    env = run_compare_countries(cube, dataset_name="country", time_column="year",
                                metric="vessels")
    assert env["skill"] == "compare_countries"
    ranking = env["data"]["ranking"]
    assert [r["country"] for r in ranking] == ["ESP", "ITA", "FRA"]  # desc by vessels
    assert env["data"]["gap_ratio"] == pytest.approx(100.0 / 40.0)
    assert env["chart"]["kind"] == "bar"
    assert verify_receipt(env["receipts"][0], secret=SECRET)["verified"] is True


class _FakeIndex:
    """Stub docsearch index: returns canned passages for any query."""
    def __init__(self, results):
        self._results = results

    def search(self, query, top_k=5):
        return self._results[:top_k]


def test_run_explain_provenance_grounds_in_docs():
    from surimi_data_mcp.engine.skills import run_explain_provenance
    idx = _FakeIndex([
        {"text": "AER data is collected under the EU DCF.", "source": "aer_readme.md",
         "ordinal": 0, "score": 9.1},
    ])
    env = run_explain_provenance(idx, dataset_id="aer-economic-2013-2023",
                                 question="where does this come from", secret=SECRET)
    assert env["skill"] == "explain_provenance"
    assert env["data"]["passages"][0]["source"] == "aer_readme.md"
    assert "aer_readme.md" in env["provenance"]["sources"]
    assert verify_receipt(env["receipts"][0], secret=SECRET)["verified"] is True


def test_run_explain_provenance_empty_is_honest():
    from surimi_data_mcp.engine.skills import run_explain_provenance
    env = run_explain_provenance(_FakeIndex([]), dataset_id="x", question="y",
                                 secret=SECRET)
    assert env["data"]["passages"] == []
    assert any("do not cover" in n or "No SURIMI" in n or "no SURIMI" in n
               for n in env["notes"]) or "No SURIMI" in env["finding"]
    assert env["receipts"] == []  # nothing to sign


def test_run_generate_report_composes_sections(cube):
    from surimi_data_mcp.engine.skills import run_generate_report
    cols = [{"name": "year", "type": "int"}, {"name": "species_code", "type": "string"},
            {"name": "biomass_kt", "type": "double"}]
    env = run_generate_report(cube, _FakeIndex([]), dataset_name="biomass",
                              time_column="year", columns=cols, focus="stocks",
                              dataset_id="surimi-biomass-wmed-empirical", secret=SECRET)
    assert env["skill"] == "generate_report"
    assert "overview" in env["data"]["sections"]
    assert env["data"]["sections"]["key_metric"]["metric"] == "biomass_kt"
    # every section number is backed by a receipt
    assert len(env["receipts"]) >= 1
    assert verify_receipt(env["receipts"][0], secret=SECRET)["verified"] is True


def test_compare_countries_notes_is_clean_list(cube):
    from surimi_data_mcp.engine.skills import run_compare_countries
    env = run_compare_countries(cube, dataset_name="country", time_column="year",
                                metric="vessels")
    # engine "ok" must normalize to [], never ['o','k']
    assert env["notes"] == [] or all(len(n) > 2 for n in env["notes"])


def test_run_generate_report_applies_period(cube):
    from surimi_data_mcp.engine.skills import run_generate_report
    cols = [{"name": "year", "type": "int"}, {"name": "species_code", "type": "string"},
            {"name": "biomass_kt", "type": "double"}]
    full = run_generate_report(cube, _FakeIndex([]), dataset_name="biomass",
                               time_column="year", columns=cols, focus="x",
                               dataset_id="biomass", secret=SECRET)
    scoped = run_generate_report(cube, _FakeIndex([]), dataset_name="biomass",
                                 time_column="year", columns=cols, focus="x",
                                 period="2014", dataset_id="biomass", secret=SECRET)
    # biomass fixture has 6 rows (3 years x 2 species); period>=2014 keeps 2.
    assert full["data"]["sections"]["overview"]["row_count"] == 6
    assert scoped["data"]["sections"]["overview"]["row_count"] == 2


def test_breakdown_count_default_collapses_case(cube):
    from surimi_data_mcp.engine.skills import run_breakdown
    env = run_breakdown(cube, dataset_name="gender", group_by="sex")
    assert env["skill"] == "breakdown"
    cats = {r["category"] for r in env["data"]["ranking"]}
    assert cats == {"male", "female", "unknown"}  # 6 raw -> 3 (Part 1 collapse)
    top = env["data"]["ranking"][0]
    assert top["category"] == "male" and top["value"] == 3  # count, not a sum
    assert env["chart"]["kind"] == "bar"
    assert len(set(env["chart"]["labels"])) == len(env["chart"]["labels"])
    assert verify_receipt(env["receipts"][0], secret=SECRET)["verified"] is True


def test_breakdown_eav_magnitude_needs_code(cube):
    from surimi_data_mcp.engine.skills import run_breakdown
    cols = [{"name": "country", "type": "string"},
            {"name": "variable_code", "type": "string"}, {"name": "value", "type": "string"}]
    env = run_breakdown(cube, dataset_name="eav", group_by="country",
                        metric="value", fn="sum", columns=cols)
    # EAV + magnitude + no variable_code -> ask, do NOT crash or fabricate
    assert env["data"]["needs"]["variable_code"]
    assert "employment_fte" in env["data"]["needs"]["variable_code"]
    assert env["receipts"] == [] and env["chart"] is None


def test_breakdown_eav_magnitude_with_code_sums(cube):
    from surimi_data_mcp.engine.skills import run_breakdown
    cols = [{"name": "country", "type": "string"},
            {"name": "variable_code", "type": "string"}, {"name": "value", "type": "string"}]
    env = run_breakdown(cube, dataset_name="eav", group_by="country", metric="value",
                        fn="sum", variable_code="employment_fte", columns=cols)
    rank = {r["category"]: r["value"] for r in env["data"]["ranking"]}
    assert rank == {"ESP": 1000.0, "FRA": 800.0}
    assert verify_receipt(env["receipts"][0], secret=SECRET)["verified"] is True


# ---------- breakdown must not sum a stock across years ----------

def test_breakdown_pins_the_latest_year_for_a_stock(cube):
    """A vessel count is semi-additive: adding 2018+2019+2020 counts hulls thrice.

    fleet.csv holds 90/95/100 for ESP across three years. Summed blind that is
    285 for a fleet that never exceeded 100.
    """
    from surimi_data_mcp.engine.skills import run_breakdown
    env = run_breakdown(cube, dataset_name="fleet", group_by="country_code",
                        metric="vessels", fn="sum",
                        columns=[{"name": "year"}, {"name": "country_code"},
                                 {"name": "vessels"}],
                        stock_cols={"vessels"}, time_column="year")
    by_cat = {r["category"]: r["value"] for r in env["data"]["ranking"]}
    assert by_cat["ESP"] == 100
    assert by_cat["ITA"] == 60
    assert by_cat["FRA"] == 40


def test_breakdown_says_which_year_it_pinned(cube):
    """Pinning silently would trade one wrong answer for another."""
    from surimi_data_mcp.engine.skills import run_breakdown
    env = run_breakdown(cube, dataset_name="fleet", group_by="country_code",
                        metric="vessels", fn="sum",
                        columns=[{"name": "year"}, {"name": "country_code"},
                                 {"name": "vessels"}],
                        stock_cols={"vessels"}, time_column="year")
    blob = " ".join(env.get("notes") or []) + " " + str(env.get("finding", ""))
    assert "2020" in blob
    assert "double-count" in blob.lower() or "latest year" in blob.lower()


def test_breakdown_year_parameter_overrides_the_default(cube):
    """Naming a year must select it, not the latest."""
    from surimi_data_mcp.engine.skills import run_breakdown
    env = run_breakdown(cube, dataset_name="fleet", group_by="country_code",
                        metric="vessels", fn="sum", year="2018",
                        columns=[{"name": "year"}, {"name": "country_code"},
                                 {"name": "vessels"}],
                        stock_cols={"vessels"}, time_column="year")
    by_cat = {r["category"]: r["value"] for r in env["data"]["ranking"]}
    assert by_cat["ESP"] == 90
    assert by_cat["ITA"] == 50


def test_breakdown_leaves_a_flow_measure_summed_across_years(cube):
    """Landings are a flow: summing years is the right answer, not a defect."""
    from surimi_data_mcp.engine.skills import run_breakdown
    env = run_breakdown(cube, dataset_name="fleet", group_by="country_code",
                        metric="vessels", fn="sum",
                        columns=[{"name": "year"}, {"name": "country_code"},
                                 {"name": "vessels"}],
                        stock_cols=set(), time_column="year")
    by_cat = {r["category"]: r["value"] for r in env["data"]["ranking"]}
    assert by_cat["ESP"] == 285   # 90 + 95 + 100


# ---------- compare_countries must understand country names ----------

def test_compare_countries_accepts_country_names(cube):
    """"Compare Spain and Italy" must compare Spain and Italy.

    country_column is defined by no dataset, so the default always resolved to
    the code column and a name filtered to nothing.
    """
    from surimi_data_mcp.engine.skills import run_compare_countries
    env = run_compare_countries(cube, dataset_name="named", time_column="year",
                                metric="landings", countries=["Spain", "Italy"])
    ranked = {r["country"]: r["value"] for r in env["data"]["ranking"]}
    assert len(ranked) == 2
    assert set(ranked) in ({"Spain", "Italy"}, {"ESP", "ITA"})
    assert max(ranked.values()) == 500


def test_compare_countries_still_accepts_codes(cube):
    """The code path is the one in use today and must not regress."""
    from surimi_data_mcp.engine.skills import run_compare_countries
    env = run_compare_countries(cube, dataset_name="named", time_column="year",
                                metric="landings", countries=["ESP", "FRA"])
    ranked = {r["country"]: r["value"] for r in env["data"]["ranking"]}
    assert len(ranked) == 2
    assert max(ranked.values()) == 500


def test_compare_countries_names_an_input_it_cannot_match(cube):
    """An unrecognised country is said so, never reported as absent data.

    "No non-null values to compare" reads as "the data does not cover this",
    which is a different and much more damaging claim than "I do not know that
    name".
    """
    from surimi_data_mcp.engine.skills import run_compare_countries
    env = run_compare_countries(cube, dataset_name="named", time_column="year",
                                metric="landings", countries=["Atlantis"])
    blob = (str(env.get("finding", "")) + " " + " ".join(env.get("notes") or [])).lower()
    assert "atlantis" in blob
    assert "no non-null values to compare" not in blob


def test_compare_countries_reports_which_column_it_matched(cube):
    """Provenance must say which column the comparison actually ran on."""
    from surimi_data_mcp.engine.skills import run_compare_countries
    env = run_compare_countries(cube, dataset_name="named", time_column="year",
                                metric="landings", countries=["Spain"])
    assert env["provenance"]["country_column"] in ("country_name", "country_code")


# --- lineage on explain_provenance (exp3) -----------------------------------
# explain_provenance is the tool an expert's "where did this number come from?"
# lands on, and until now it only searched prose. When the docs say nothing it
# returned "No SURIMI documentation matched" plus a do-not-guess note, so the
# manifest that records the actual source file, its sha256, its row count and
# its year span never reached the model. The lesson this repo keeps relearning
# is that a fact the model never sees cannot steer it: lineage has to ride on
# the tool RESULT.

def _manifest(tmp_path, key, entry):
    import json
    p = tmp_path / "sources_manifest.json"
    p.write_text(json.dumps({key: entry}), encoding="utf-8")
    return str(p)


def test_explain_provenance_reports_manifest_lineage(tmp_path, monkeypatch):
    """With no matching doc, the manifest entry is still a real answer."""
    from surimi_data_mcp.provenance import lineage
    from surimi_data_mcp.engine.skills import run_explain_provenance
    from surimi_data_mcp.datasets import catalog

    ds = catalog.DATASETS[0]
    from surimi_data_mcp.engine.cube import _sanitize_name
    key = _sanitize_name(ds["table"])
    entry = {"file": "some_source.csv", "sha256": "ab" * 32, "n_rows": 21077,
             "first": 1904, "last": 2023, "agency": "ICES", "original_url": None,
             "missing": []}
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", _manifest(tmp_path, key, entry))
    lineage.reset()

    env = run_explain_provenance(_FakeIndex([]), dataset_id=ds["id"], secret=SECRET)

    lin = env["data"]["lineage"]
    assert lin["file"] == "some_source.csv"
    assert lin["sha256"] == "ab" * 32
    assert lin["n_rows"] == 21077
    assert (lin["first"], lin["last"]) == (1904, 2023)
    # The finding must STATE the lineage, not report an absence.
    assert "some_source.csv" in env["finding"]
    assert "No SURIMI documentation matched" not in env["finding"]
    # And it must not tell the model to treat this as uncovered.
    assert not any("do not cover" in n for n in env["notes"])
    lineage.reset()


def test_explain_provenance_without_manifest_is_unchanged(tmp_path, monkeypatch):
    """No manifest entry: the honest refusal stays exactly as it was."""
    from surimi_data_mcp.provenance import lineage
    from surimi_data_mcp.engine.skills import run_explain_provenance
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", _manifest(tmp_path, "other_key", {}))
    lineage.reset()

    env = run_explain_provenance(_FakeIndex([]), dataset_id="x", question="y",
                                 secret=SECRET)
    assert env["data"]["passages"] == []
    assert env["data"].get("lineage") is None
    assert "No SURIMI" in env["finding"]
    assert any("do not cover" in n for n in env["notes"])
    lineage.reset()
