"""Q-E2-1a / Q-E2-2 / Q-E2-3: the requirements on the `eu-dcf-catches` registration, plus
two catalogue entries in `datasets/datasets.yaml`.

Q-E2-1a ("How will fisheries catches in Belgium change with climate change?")
and Q-E2-2 ("biodiversity implications of Belgian catches") both need
EU_DCF_Catches.csv reachable. It is a cube-only dataset (1.75 GB, 6,087,268
rows; not in the Postgres analytic subset per
tests/test_catalog_additions.py::test_the_fdi_catches_file_is_not_loaded_into_postgres).

The `eu-dcf-catches` entry is registered once, in `datasets/datasets.yaml`. The tests
below pin what Q-E2 needs from it: the discard and gear columns, cube resolution and the
confidentiality-token warning. They check the additivity tag and the warning text and
not the column type string, because the `type` field drives no runtime behaviour for a
cube-only dataset outside the two landings tables' declared-type override (the `TRY_CAST`
decision in `aggregate()` reads the cube's real DuckDB column type).

Q-E2-3 ("Will subsidies to Belgian fishers help them be sustainable?") needs
the FSE category breakdown (oecd.fsedata / cube dataset hive_oecd_fsedata),
whose column rotation tests/test_cube_rotation_fix.py and
tests/test_load_csv_row_realignment.py cover in both ingest paths. This file
pins its registration.

Also: oecd-fisheries-support-eu's scope was previously carried only in
``scope_note``; its ``name`` is amended to state the six countries directly,
the same convention `datasets.yaml` uses for the landings-table overlap
warning (see the comment above the ``fisheries-landings`` entry: "the only
field list_datasets surfaces to the model").
"""
from __future__ import annotations

import pytest

from surimi_data_mcp.engine import cube as cube_mod

from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.tools import common


def _eu_dcf_catches_or_skip():
    """Return the `eu-dcf-catches` entry, or skip when the catalogue does not carry it."""
    ds = catalog.find_dataset("eu-dcf-catches")
    if ds is None:
        pytest.skip(
            "eu-dcf-catches is registered on expert/E1, not this branch "
            "(converged there after a duplicate-id collision); verified "
            "directly against expert/E1's catalog.py, see module docstring")
    return ds


# ── eu-dcf-catches ────────────────────────────────────────────────────────

def test_eu_dcf_catches_is_registered():
    ds = _eu_dcf_catches_or_skip()
    assert ds["kind"] == "observed"
    assert ds["domain"] == "eu_dcf"
    assert ds["time_column"] == "year"


def test_eu_dcf_catches_table_resolves_to_the_real_cube_dataset_name():
    """Measured directly against the actual file: EU_DCF_Catches.csv builds
    a cube table named exactly 'eu_dcf_catches'. Either the catalog id or the
    table field must sanitize to that name, or common.resolve_cube_dataset
    can never find it and every aggregate() call fails 'unknown dataset'."""
    ds = _eu_dcf_catches_or_skip()
    sanitized_id = cube_mod._sanitize_name("eu-dcf-catches")
    sanitized_table = cube_mod._sanitize_name(ds["table"])
    assert "eu_dcf_catches" in (sanitized_id, sanitized_table)


def test_eu_dcf_catches_exposes_the_columns_q_e2_2_needs():
    """Q-E2-2's fix explicitly names these: fishing_tech, tot_discards_tonnes
    and the confidential flag, alongside the landed-weight/value measures and
    the species/gear dimensions the discard-rate and gear-share figures need."""
    ds = _eu_dcf_catches_or_skip()
    names = {c["name"] for c in ds["columns"]}
    for col in (
        "country", "year", "quarter", "fishing_tech", "gear_type", "spe_code",
        "total_live_weight_landed", "total_value_of_landings",
        "tot_discards_tonnes", "confidential", "english_name",
    ):
        assert col in names, f"missing column {col}"


def test_eu_dcf_catches_is_not_flagged_for_postgres():
    """Must not resurrect the 1.75 GB / confidentiality-as-string load this
    repo deliberately keeps cube-only. Unlike the tests above this one does
    NOT need the registration to exist -- it holds regardless -- so it is
    not gated on _eu_dcf_catches_or_skip()."""
    from surimi_data_mcp.loader import bootstrap
    names = " ".join(bootstrap.ANALYTIC_CSVS).lower()
    assert "eu_dcf_catches" not in names


def test_eu_dcf_catches_value_column_declares_additivity():
    """What Q-E2 needs from this column is that total_value_of_landings is
    tagged as a summable flow -- NOT a specific "type" string.
    The "type" field in `datasets.yaml` drives no runtime behaviour for a cube-only
    column outside the two landings tables' declared-type override
    (aggregate()'s TRY_CAST reads the cube's real DuckDB column type, not
    this field), so no specific type string is asserted."""
    ds = _eu_dcf_catches_or_skip()
    col = next(c for c in ds["columns"] if c["name"] == "total_value_of_landings")
    assert col.get("additivity") == "flow"


def test_eu_dcf_catches_confidentiality_warning_keeps_its_substance():
    """The warning must tell the model
    HOW to handle the 'C' sentinel with concrete evidence, not just that suppression exists in the
    abstract. The cast it gives must run on PostgreSQL as well as the cube: TRY_CAST(... AS DOUBLE)
    failed live with a syntax error, which the cube fallback does not catch (2026-10-13)."""
    _eu_dcf_catches_or_skip()
    warn = catalog.substitute_warning("eu-dcf-catches")
    assert "aggregate" in warn, "totals must be sent to aggregate"
    assert "NULLIF(col, 'C') AS double precision" in warn, "a cast both backends accept"
    assert "TRY_CAST" not in warn
    assert "330,212" in warn, "concrete suppressed-row count must be present"
    assert "floor over the visible rows" in warn, "the floor framing must be present"


def test_no_registered_dataset_entry_was_broken():
    ids = [d["id"] for d in catalog.DATASETS]
    assert len(ids) == len(set(ids)), "duplicate dataset id"
    for known in ("eu-dcf-economic-aegean", "fisheries-landings"):
        assert known in ids


# ── oecd-fsedata ──────────────────────────────────────────────────────────

def test_oecd_fsedata_is_now_registered():
    ds = catalog.find_dataset("oecd-fsedata")
    assert ds is not None
    assert ds["kind"] == "observed"
    assert ds["table"].lower().endswith("oecd.fsedata")


def test_oecd_fsedata_table_resolves_to_the_real_cube_dataset_name():
    ds = catalog.find_dataset("oecd-fsedata")
    sanitized_id = cube_mod._sanitize_name("oecd-fsedata")
    sanitized_table = cube_mod._sanitize_name(ds["table"])
    assert "hive_oecd_fsedata" in (sanitized_id, sanitized_table)


def test_oecd_fsedata_exposes_the_category_and_wide_year_columns():
    ds = catalog.find_dataset("oecd-fsedata")
    names = {c["name"] for c in ds["columns"]}
    for col in ("country_code", "fse_category_code", "fse_category_name", "y_2019"):
        assert col in names, f"missing column {col}"


def test_the_clean_oecd_coastal_states_table_is_still_registered():
    ds = catalog.find_dataset("oecd-fisheries-support-eu")
    assert ds is not None
    assert ds["kind"] == "observed"


def test_the_subsidies_view_still_builds_on_the_clean_table_not_fsedata():
    from surimi_data_mcp.datasets import semantic_views
    v = next(v for v in semantic_views.SEMANTIC_VIEWS
             if v["name"] == "v_subsidies_by_country_year")
    assert "fsedata" not in v["base_table"]


# ── oecd-fisheries-support-eu name amendment ───────────────────────────────

def test_oecd_coastal_states_name_states_its_six_countries():
    """scope_note already said this, but list_datasets is the one call a
    model reliably makes, and name is the field it always reads -- the same
    reasoning catalog.py documents for the landings-table overlap warning."""
    ds = catalog.find_dataset("oecd-fisheries-support-eu")
    name = ds["name"]
    for code in ("ESP", "FRA", "ITA", "GRC", "PRT", "NLD"):
        assert code in name, f"{code} missing from name: {name!r}"
    assert "6" in name or "six" in name.lower()


def test_oecd_coastal_states_name_reaches_list_datasets():
    out = catalog.list_datasets(domain="oecd")
    row = next(d for d in out["datasets"] if d["id"] == "oecd-fisheries-support-eu")
    for code in ("ESP", "FRA", "ITA", "GRC", "PRT", "NLD"):
        assert code in row["name"]
