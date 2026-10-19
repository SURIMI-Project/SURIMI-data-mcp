"""What SURIMI's documentation says the data was used for (2026-10-12).

Asked what the data was used for, the chat searched the documents five or six times, found the wiki's
description of what the models pass to each other during a simulation, and offered that as the use of
the data. Nothing it could reach said that the use of each dataset is not documented. Checked against the
data lake's own READMEs: only the CMSY++ input files (9_Model Inputs) say what they were used for. The
catalogue now states that, through list_datasets and explain_provenance, and the document search points
there.
"""
from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.tools import explain


def test_list_datasets_states_what_is_and_is_not_documented_about_use():
    note = catalog.list_datasets()["documented_use"]
    assert "CMSY++" in note and "9_Model Inputs" in note
    assert "not" in note and "README" in note


def test_the_note_rides_on_a_filtered_listing_too():
    assert catalog.list_datasets(domain="eu_dcf")["documented_use"] == catalog.DOCUMENTED_USE


def test_explain_provenance_carries_the_documented_use():
    from surimi_data_mcp.engine.skills import run_explain_provenance

    class _Index:
        def search(self, query, top_k=5):
            return []

    env = run_explain_provenance(_Index(), dataset_id="eu-dcf-catches", question="what was it used for")
    assert env["data"]["documented_use"] == catalog.DOCUMENTED_USE


def test_the_document_search_points_to_the_documented_use(monkeypatch):
    from surimi_data_mcp.knowledge import docsearch
    from surimi_data_mcp import server

    monkeypatch.setattr(docsearch, "search", lambda q, top_k=5: [])
    out = explain.search_documents("what was the data used for")
    assert "documented_use" in out["grounding"] and "list_datasets" in out["grounding"]
    assert "documented_use" in explain.search_documents.__doc__
