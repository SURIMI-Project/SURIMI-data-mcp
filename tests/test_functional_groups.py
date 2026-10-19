"""Q-E6-1 / Q-E6-5: "which species make up a group" has no answer today.

EwE_functional-group_species.csv (references/SURIMI-western_med/data-raw/)
names 59 Ecopath functional groups and, for the fished ones, the species that
belong to each. The tool `data_list_functional_groups` exposes it, so a question like
"which species are in the small-demersal group" or "what is small demersal
fish" has an answer. Q-E6-5 needs it to resolve the phrase "small demersal"
to a concrete species list.

Bundled the same way species.csv already is (data/vocabularies/, checked into
this repo so it works standalone, no monorepo assumption) rather than loaded
into Postgres: 111 data rows, a static reference table, not something a model
should ever need to SUM or GROUP BY.

The source file is left verbatim, including its own data-quality quirks: the
literal "fishesl" typo on group 31, and group numbers reused for two
different names ("Suprabenthos" and "Spottail mantis shrimp" share EwE_Group_No
10). This tool exposes the file. It does not correct it.
"""
from surimi_data_mcp.knowledge import lookups
from surimi_data_mcp.tools import explain


def test_no_query_lists_every_group_name():
    out = lookups.list_functional_groups()
    assert out["found"] is True
    assert "Small demersal fishesl (continental shelf)" in out["groups"]
    # 59 distinct EwE_Group_Name values across the 111 data rows -- matching
    # the 59 functional groups the Ecopath NWMed model itself declares (see
    # Q-E6-1). Some group numbers repeat with two different names, so this is
    # not simply "number of distinct EwE_Group_No".
    assert len(out["groups"]) == 59


def test_small_demersal_resolves_to_its_species_verbatim_typo_and_all():
    out = lookups.list_functional_groups("small demersal")
    assert out["found"] is True
    assert out["matched_group_names"] == ["Small demersal fishesl (continental shelf)"]
    names = {s["scientific_name"] for s in out["species"]}
    assert "Lepidotrigla cavillone" in names
    assert "Serranus hepatus" in names
    assert "Diplodus sp." in names
    assert out["n_species"] == 3


def test_match_is_case_insensitive_and_substring():
    out = lookups.list_functional_groups("SMALL DEMERSAL")
    assert out["found"] is True
    assert out["n_species"] == 3


def test_a_group_with_no_species_rows_is_found_but_carries_no_species():
    # Phytoplankton (group 2) has no Taxon_No/species columns filled in.
    out = lookups.list_functional_groups("phytoplankton")
    assert out["found"] is True
    assert out["species"] == []
    assert out["n_species"] == 0


def test_unknown_group_name_is_reported_not_guessed():
    out = lookups.list_functional_groups("nonexistent group xyz")
    assert out["found"] is False
    assert out["species"] == []


def test_source_is_named_so_the_answer_is_traceable():
    out = lookups.list_functional_groups("hake")
    assert "ewe_functional_groups.csv" in out["source"]


# --- 2026-09-28: the group's biomass series is on the MODEL server ----------
# The tool told every caller to resolve "small demersal" to species "before
# querying MEDITS". MEDITS is a trawl-survey catch-rate index for those
# species, not biomass, while the model server serves the group's biomass as
# EcoOcean bd30cm. On the re-grounded benchmark all three models answered
# E5-1, E5-7 and E11 from MEDITS or from a survey table holding no small
# demersals at all, and never reached bd30cm.

def test_small_demersal_points_to_the_model_servers_biomass_series():
    out = lookups.list_functional_groups("small demersal")
    m = out["model_series"]
    assert m["server"] == "model"
    assert m["variable"] == "bd30cm"
    assert "model_trend" in m["tools"] and "model_time_series" in m["tools"]
    assert "Mediterranean" in m["how"]


def test_the_survey_route_is_named_as_an_index_not_biomass():
    note = lookups.list_functional_groups("small demersal")["survey_note"].lower()
    assert "medits" in note and "index" in note and "not biomass" in note


def test_a_group_without_a_model_series_gets_no_pointer():
    out = lookups.list_functional_groups("phytoplankton")
    assert out["found"] is True
    assert "model_series" not in out


def test_the_tool_description_no_longer_sends_every_group_to_medits():
    import inspect
    from surimi_data_mcp import server
    doc = inspect.getdoc(explain.data_list_functional_groups)
    assert "bd30cm" in doc
    assert "before querying MEDITS" not in doc
