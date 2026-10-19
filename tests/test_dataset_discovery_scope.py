"""A narrow dataset must say what it is narrow ABOUT, and name the wider one.

Q-E2-3, expert benchmark 2026-09-18: "Will subsidies to Belgian fishers help
them be sustainable?" The assistant checked v_subsidies_by_country_year (six
countries: ESP FRA ITA GRC PRT NLD) and the OECD coastal-states table (the same
six), found no Belgium in either, and declined.

Both claims were true. The conclusion was false: aer_economic_national_level
holds 11 Belgian totdirsub rows, verified live. The judge scores that
abstain_wrong_reason -- the user is misled about the data even though they got
no wrong number.

This is a discovery defect, not a capability one. Nothing the assistant could
call said "this view covers six countries; national subsidies for every EU state
are in aer-economic-national-level under variable_code totdirsub". list_datasets
returned id, name, table and domain, and no dataset declared its own limits.

The scope rule from JUDGE-EXPERT.md is what makes this the server's problem: a
scoped claim is consistent if it is true at the scope it names, so the model was
reasoning correctly from what it was told. It was told too little.
"""
from __future__ import annotations

from surimi_data_mcp.datasets import catalog


def test_the_narrow_subsidies_view_declares_its_scope():
    ds = catalog.find_dataset("v-subsidies-by-country-year")
    assert ds.get("scope_note"), "the narrow view must declare what it does not cover"


def test_the_narrow_subsidies_view_names_where_the_rest_lives():
    note = catalog.find_dataset("v-subsidies-by-country-year")["scope_note"]
    assert "aer-economic-national-level" in note, note
    assert "totdirsub" in note, note


def test_the_oecd_coastal_states_table_declares_its_scope_too():
    """The second narrow source the assistant checked."""
    ds = catalog.find_dataset("oecd-fisheries-support-eu")
    assert ds.get("scope_note"), "this table also covers only six countries"


def test_list_datasets_surfaces_scope_and_kind():
    """A field nothing can read does not change an answer."""
    out = catalog.list_datasets()
    row = next(d for d in out["datasets"] if d["id"] == "v-subsidies-by-country-year")
    assert row.get("scope_note"), "scope_note must reach the model through discovery"
    assert row.get("kind") == "observed", "kind must reach the model too"


def test_the_modelled_dataset_is_visible_as_modelled_in_discovery():
    out = catalog.list_datasets()
    row = next(d for d in out["datasets"] if d["id"] == "surimi-sales-aegean-simulated")
    assert row["kind"] == "modelled"


def test_datasets_without_a_scope_limit_need_no_note():
    """Only genuinely narrow datasets carry one; a note on everything is noise."""
    out = catalog.list_datasets()
    row = next(d for d in out["datasets"] if d["id"] == "aer-economic-2013-2023")
    assert not row.get("scope_note")
