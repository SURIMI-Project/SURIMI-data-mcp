"""Helpers shared by the tool modules: the database and query clients, the lookup of a dataset in the
cube, and the audit links added to results.
"""
from surimi_data_mcp.datasets import catalog, wide_years
from surimi_data_mcp.engine import cube as cube_module
from surimi_data_mcp.engine.databases import TrinoClient, default_client
from surimi_data_mcp.engine.sql_tools import (
    CubeFallbackClient,
)
from surimi_data_mcp.mcp_server import log
from surimi_data_mcp.provenance.audit_link import audit_url_for

_trino: TrinoClient | None = None


def get_trino() -> TrinoClient:
    global _trino
    if _trino is None:
        _trino = default_client()
    return _trino


def attach_audit_urls(env: dict) -> dict:
    """Add `audit_urls` (one link per calculation record) next to the envelope's `receipts`, never inside
    a receipt dict, because a signed record that changes no longer verifies.

    `audit_urls[i]` pairs with `receipts[i]` by index. A link that cannot be made (for example when
    `SURIMI_PUBLIC_BASE_URL` is unset) stays None and is not filtered out, since removing it would shift the pairing.

    Two envelope shapes exist. Skills carry a plural `receipts`, and the aggregate engine returns a
    singular `receipt`. The function reads both, so `query_metric` gets an audit link too."""
    receipts = list(env.get("receipts") or [])
    single = env.get("receipt")
    if single and single not in receipts:
        receipts.append(single)
    env["audit_urls"] = [audit_url_for(r) for r in receipts]
    return env


def wide_year_view(ds, cube, cube_ds):
    """Expose a wide year table as (view_name, "year", empty_years) or None.

    surimi-biomass-medits carries y_1994..y_2021 as 28 COLUMNS and records
    time_column=None, so every time-aware skill refused it with "this dataset
    has no time dimension" while holding 28 of them. oecd-fsedata is the same.
    Those are the tables the decline questions are about, so each client was
    left to hand-roll the trend and each invented a different method.

    The fold happens once, here, on a view that the calculation record can quote.
    """
    # Read the cube's columns and not the catalogue's, because the unpivot
    # must name columns that exist in the cube.
    cols = wide_years.cube_columns(cube, cube_ds)
    if not cols:
        cols = [c["name"] for c in (ds.get("columns") or [])]
    years = wide_years.year_columns(cols)
    if not years:
        return None
    dims = [c for c in cols if c not in {col for _, col in years}]
    view = f"v_wide_{cube_ds}"
    sql = wide_years.unpivot_sql(cube_ds, dims, years, "value")
    try:
        cube.con.execute(wide_years.create_view_sql(view, sql))
    except Exception as exc:  # a cube that cannot host a view is not an error
        log(f"[skill] wide-year view failed for {cube_ds}: {exc}")
        return None
    return view, "year", years


def resolve_for_skill(dataset_id):
    """(ds_meta, cube, cube_dataset_name) or raises a clear ValueError."""
    ds = catalog.find_dataset(dataset_id)
    if ds is None:
        raise ValueError(f"Dataset '{dataset_id}' not found. Call list_datasets.")
    cube = cube_module.get_cube()
    cube_ds = resolve_cube_dataset(ds.get("table") or dataset_id, cube)
    return ds, cube, cube_ds


def query_client() -> CubeFallbackClient:
    """The query_data client: the primary backend + a DuckDB-cube fallback.

    On a table-not-found for a cube-resident table (the surimi biomass/sales
    tables, absent from the EDITO Postgres analytic subset) the query is served
    from the cube superset instead of a false "can't retrieve". Uses the same
    lazy get_cube singleton the aggregate/breakdown tools use, with the cube's
    own sanitizer so the FQN->table mapping never drifts. The SAME wrapper backs
    the `verify` recompute path, so a record served from the cube verifies against the cube.
    """
    return CubeFallbackClient(
        get_trino(), cube_module.get_cube(), cube_module._sanitize_name,
    )


# ── Dataset names in the DuckDB cube ──
# `cube_module.get_cube()` opens or builds the cube on first use, so importing
# this module does not load the data.


def resolve_cube_dataset(dataset: str, cube) -> str:
    """Bridge a catalog id / Trino FQN to the cube's dataset name.

    ``list_datasets`` gives the chat catalog ids (``eu-dcf-economic-aegean``)
    and Trino FQNs (``hive.eu_dcf.X``), but the cube keys datasets by the
    sanitized CSV stem. For the demo exports that stem is ``hive_eu_dcf_X``;
    for a table registered straight over a raw-lake file (no ``hive_``-prefixed
    export copy) the stem is just the file's own name, e.g.
    ``eu_dcf_effort_by_country`` for table ``hive.eu_dcf.eu_dcf_effort_by_country``.
    Without this bridge EVERY aggregate call the chat makes fails with "unknown
    dataset", because the model only knows catalog ids. Resolution order: the
    name as-is (already a cube name) -> the sanitized input (a raw FQN/table
    the model copied) -> the sanitized catalog ``table`` for that id -> the
    sanitized LAST segment of that table (the raw-lake-stem case). Returns the
    input unchanged when nothing matches, so ``_aggregate`` raises its own
    clear error instead of this silently guessing a dataset.
    """
    try:
        names = set(cube.datasets())
    except Exception:
        names = set()
    if dataset in names:
        return dataset
    candidates = [cube_module._sanitize_name(dataset)]
    ds = catalog.find_dataset(dataset)
    if ds and ds.get("table"):
        candidates.append(cube_module._sanitize_name(ds["table"]))
        candidates.append(cube_module._sanitize_name(ds["table"].split(".")[-1]))
    for cand in candidates:
        if cand in names:
            return cand
    return dataset
