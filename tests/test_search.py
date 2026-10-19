"""Tests for the search and explain tools."""
import pytest


def test_explain_indicator_known():
    from surimi_data_mcp.knowledge.lookups import explain_indicator
    result = explain_indicator("direct_transfers_meur")
    assert result["found"] is True
    assert result["definition"] is not None


def test_explain_indicator_by_dataset():
    from surimi_data_mcp.knowledge.lookups import explain_indicator
    result = explain_indicator("employment_fte", dataset_id="eu-dcf-economic-aegean")
    assert result["found"] is True


def test_explain_indicator_unknown():
    from surimi_data_mcp.knowledge.lookups import explain_indicator
    result = explain_indicator("completely_made_up_xyz")
    assert result["found"] is False


def test_explain_indicator_returns_dataset_context():
    from surimi_data_mcp.knowledge.lookups import explain_indicator
    result = explain_indicator("biomass_kt")
    assert result["found"] is True
    assert "dataset_id" in result


def test_explain_indicator_landings_has_verified_unit():
    from surimi_data_mcp.knowledge.lookups import explain_indicator
    result = explain_indicator("landings")
    assert result["found"] is True
    assert result["unit"] == "tonnes (live weight)"
    assert "landings" in result["definition"].lower()
    assert result["dataset_id"] in ("fisheries-landings", "fisheries-landings-divisions")


# ---------- explain_indicator must not pass a non-answer off as a definition ----------

def test_explain_indicator_does_not_call_a_bare_column_defined():
    """A column that exists but has no description is not an explained one.

    The fallback returned found:true with "Column 'x' exists in dataset 'y'.
    No detailed description available.", which a model relays as though the
    indicator had been defined. found:true must mean a definition was found.
    """
    from surimi_data_mcp.knowledge.lookups import explain_indicator
    result = explain_indicator("value", dataset_id="aer-economic-national-level")
    if result["found"]:
        assert "No detailed description available" not in (result["definition"] or "")
    else:
        assert result["definition"] is None


def test_explain_indicator_says_the_column_exists_without_claiming_a_definition():
    """Knowing where a column lives is useful and must survive the fix --
    reported as location, not as a definition."""
    from surimi_data_mcp.knowledge.lookups import explain_indicator
    result = explain_indicator("value", dataset_id="aer-economic-national-level")
    assert result.get("column_exists_in") == "aer-economic-national-level"


@pytest.fixture
def eav_lake(tmp_path, monkeypatch):
    """A cube holding the AER national table's shape, code and label columns.

    The real lake lives in data/exports/, which is gitignored, so a test that
    reads the live cube passes in the monorepo and fails here. Build the two
    rows the vocabulary needs instead.
    """
    import csv as _csv

    from surimi_data_mcp.engine import cube as cube_module
    from surimi_data_mcp.knowledge import lookups as search_module

    root = tmp_path / "lake"
    root.mkdir()
    with open(root / "hive_eu_dcf_aer_economic_national_level.csv", "w",
              newline="") as fh:
        _csv.writer(fh).writerows([
            ["country_name", "country_code", "year", "variable_group",
             "variable_name", "variable_code", "value", "unit"],
            ["Ireland", "IRL", "2010", "Income", "Operating subsidies",
             "totdirsub", "984251", "euro"],
            ["Belgium", "BEL", "2020", "Effort", "Fishing days",
             "totfishdays", "13157", "day"],
        ])
    monkeypatch.setenv("SURIMI_DATA_DIR", str(root))
    monkeypatch.setenv("SURIMI_CUBE_DB", ":memory:")
    monkeypatch.setattr(cube_module, "_CUBE", None, raising=False)
    # The vocabulary is cached after its first read.
    monkeypatch.setattr(search_module, "_EAV_CACHE", None, raising=False)
    return root


def test_explain_indicator_knows_a_real_eav_variable_code(eav_lake):
    """totdirsub resolves to its label and unit, where it used to resolve to
    nothing at all.

    Updated 2026-09-15: this originally asserted found:true with the label as
    the definition. A reviewer had asked what an operating subsidy IS and got
    the label back, so found:true was overclaiming. The resolution is the win
    and is kept; the claim that it is a definition is not.
    """
    from surimi_data_mcp.knowledge.lookups import explain_indicator
    result = explain_indicator("totdirsub")
    assert result["found"] is False
    assert "operating subsidies" in result["label"].lower()
    assert result["unit"] == "euro"


def test_explain_indicator_eav_code_is_case_insensitive(eav_lake):
    from surimi_data_mcp.knowledge.lookups import explain_indicator
    assert explain_indicator("TOTFISHDAYS")["label"] == "Fishing days (Effort)"


def test_explain_indicator_still_returns_not_found_for_nonsense():
    from surimi_data_mcp.knowledge.lookups import explain_indicator
    result = explain_indicator("zzz_not_a_real_indicator")
    assert result["found"] is False
    assert result["definition"] is None


def test_eav_code_is_labelled_not_defined(eav_lake):
    """A code's printed label is not a definition of the term.

    The reviewer who asked "you never define what an operating subsidy is" gets
    back "Operating subsidies (Income)", which is the label with its accounting
    group. That says which line of the accounts a number sits on. It does not
    say what an operating subsidy IS, and found:true claims it does. The file's
    own comment reserves found:true for a real definition; this branch ran
    before it and broke that rule for all 33 EAV codes.
    """
    from surimi_data_mcp.knowledge.lookups import explain_indicator
    r = explain_indicator("totdirsub")
    assert r["found"] is False
    assert r["definition"] is None
    assert r["label"] == "Operating subsidies (Income)"
    assert r["unit"] == "euro"
    assert "no definition" in r["note"].lower()


def test_eav_code_still_names_where_the_number_lives(eav_lake):
    """The useful half survives: which table and which accounting line."""
    from surimi_data_mcp.knowledge.lookups import explain_indicator
    r = explain_indicator("totdirsub")
    assert r["column_exists_in"] == "aer-economic-national-level"
    assert r["source"]
