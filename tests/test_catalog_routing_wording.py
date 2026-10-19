"""What list_datasets tells a model decides which table it opens.

Traced benchmark run, 2026-09-25 (Sonnet, MCP only):
- Q-E2-3 (Belgian subsidies): 18 data-server calls, never opened
  oecd-fsedata. Its name presented it as the detail behind the six-country
  table, and v-subsidies' scope note sent Belgium to the AER table only.
  oecd-fsedata covers about 43 countries, Belgium included.
- Q-E1-5, Q-E9-3 (depleted stocks, the "red zone"): the ICES table was never
  queried for Blim. What it answers lived in not_a_substitute_for, which
  list_datasets does not surface; it had no scope_note at all.
"""
from surimi_data_mcp.datasets import catalog


def _listed(dataset_id):
    return next(d for d in catalog.list_datasets()["datasets"] if d["id"] == dataset_id)


def test_fsedata_says_it_covers_belgium_and_many_more_countries():
    name = _listed("oecd-fsedata")["name"]
    assert "Belgium" in name and "43" in name


def test_the_six_country_subsidy_view_points_to_fsedata_as_well():
    note = _listed("v-subsidies-by-country-year")["scope_note"]
    assert "oecd-fsedata" in note


def test_the_ices_table_says_what_status_questions_it_answers():
    note = _listed("ices-stock-assessment-latest")["scope_note"].lower()
    for term in ("overfished", "red zone", "blim", "flim", "fmsy"):
        assert term in note, term


def test_the_ices_note_sends_the_western_mediterranean_elsewhere():
    note = _listed("ices-stock-assessment-latest")["scope_note"]
    assert "Mediterranean" in note and "model_run_stock_status" in note
