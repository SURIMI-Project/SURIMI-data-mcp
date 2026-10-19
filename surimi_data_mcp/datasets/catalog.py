"""The SURIMI dataset catalogue and the lookups over it.

The datasets are declared in datasets.yaml beside this module. The lookups accept a dataset id,
a table's full name, its short name, or a partial match.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml


# The datasets are declared in datasets.yaml next to this module, with a comment beside each entry
# that needs one. This module holds the lookups over them.
_DATASETS_FILE = Path(__file__).resolve().parent / "datasets.yaml"
DATASETS: list[dict[str, Any]] = yaml.safe_load(_DATASETS_FILE.read_text(encoding="utf-8"))

# `origin` says whether a dataset is observed or modelled. It is copied from the dataset's `kind`
# unless the entry sets it, and `list_datasets` and `query_metric` return it.
for _d in DATASETS:
    _d.setdefault("origin", _d.get("kind", ""))
del _d

_NORM_RE = re.compile(r"[._\-]")


def _norm(s: str) -> str:
    return _NORM_RE.sub("", s.lower())


def find_dataset(dataset_id: str) -> Optional[dict[str, Any]]:
    """Return the catalogue entry that matches `dataset_id`, or None.

    Case, dots, dashes and underscores are ignored. It tries the id, the table name and its last one or
    two segments first, and then a substring match for names longer than four characters.
    """
    needle = _norm(dataset_id)
    for d in DATASETS:
        if _norm(d["id"]) == needle:
            return d
        if _norm(d["table"]) == needle:
            return d
        parts = d["table"].split(".")
        if len(parts) == 3 and _norm(parts[2]) == needle:
            return d
        if len(parts) == 3 and _norm(f"{parts[1]}.{parts[2]}") == needle:
            return d
    for d in DATASETS:
        if len(needle) > 4 and needle in _norm(d["id"]):
            return d
        if len(needle) > 4 and needle in _norm(d["table"]):
            return d
    return None


def mark_unservable(rows, resolve, cube_names):
    """Return copies of `rows` in which datasets missing from the cube carry `cube_servable` False and a `not_servable` note.

    `resolve` maps a table name to the cube's dataset name and `cube_names` is the set the cube holds.
    If `cube_names` is empty or `resolve` raises for a row, that row is returned unchanged.
    """
    if not cube_names:
        return list(rows)
    out = []
    for row in rows:
        entry = dict(row)
        try:
            servable = resolve(entry.get("table") or entry["id"]) in cube_names
        except Exception:
            out.append(entry)
            continue
        if not servable:
            # The check only sees the cube, so the note speaks for the tools the cube backs
            # (aggregate, breakdown, query_metric) and leaves query_data open: a table can be
            # absent from the cube and still be readable through query_data.
            entry["cube_servable"] = False
            entry["not_servable"] = (
                f"The cube does not hold {entry['id']}, so aggregate, breakdown and "
                f"query_metric will fail on it. query_data over its table may still "
                f"work: try it. If query_data reports that the relation does not "
                f"exist, the data is not loaded in this deployment, and then say "
                f"SURIMI holds it and this server cannot reach it."
            )
        out.append(entry)
    return out


def source_for_dataset(dataset_id: str) -> Optional[dict[str, Any]]:
    """Return the manifest entry for a catalogue dataset, or None if the dataset or its source is unknown.

    It follows dataset, table, sanitized cube name, manifest, the same walk `list_datasets` makes.
    """
    from surimi_data_mcp.provenance import lineage
    from surimi_data_mcp.engine.cube import _sanitize_name

    ds = find_dataset(dataset_id)
    if ds is None:
        return None
    return lineage.get_source_for(_sanitize_name(ds["table"]))


def _temporal_note(source: Optional[dict[str, Any]]) -> str:
    """Return a sentence with the first and last year the manifest scan found, and any missing years in between.

    It returns '' when the source has no year span, and `list_datasets` then leaves the field out.
    """
    if not source:
        return ""
    first, last = source.get("first"), source.get("last")
    if first is None or last is None:
        return ""
    note = f"Source data covers {first}-{last}."
    missing = source.get("missing") or []
    if missing:
        note += f" Missing year(s) in that range: {missing}."
    return note


# What SURIMI's own documentation says the data was used for, read from the READMEs in the data lake
# on 2026-10-12. A README that names a dataset's use changes this text.
DOCUMENTED_USE = (
    "SURIMI does not document which model or analysis used each dataset. Of the data lake's READMEs, "
    "only the CMSY++ input files (9_Model Inputs, Northern Spain GSA06) say what they were used for: "
    "catch time series, an abundance index and stock parameters prepared as input to the CMSY++ stock "
    "assessment. No other README, and no entry in this catalogue, records a dataset's use. The project "
    "documents describe the model chain (which model passes biomass, catches or sales to which), not "
    "which dataset fed it, so that chain is not the documented use of any dataset."
)


def list_datasets(domain: Optional[str] = None) -> dict[str, Any]:
    """Return the catalogue as {"datasets": [...], "documented_use": ...}, optionally limited by `domain`.

    `domain` matches as a substring of a dataset's domain, table or name. Each entry carries
    `has_uncertainty_column` and leaves out the optional fields that are empty.
    """
    from surimi_data_mcp.provenance import lineage
    from surimi_data_mcp.engine.cube import _sanitize_name

    filtered = DATASETS
    if domain:
        domain_lower = domain.lower()
        filtered = [
            d for d in DATASETS
            if domain_lower in d["domain"].lower()
            or domain_lower in d["table"].lower()
            or domain_lower in d["name"].lower()
        ]
    datasets = []
    for d in filtered:
        entry = {k: v for k, v in (
            ("id", d["id"]), ("name", d["name"]), ("table", d["table"]),
            ("domain", d["domain"]), ("kind", d.get("kind", "")),
            ("origin", d.get("origin", "")),
            ("scope_note", d.get("scope_note", "")),
            ("notes", _temporal_note(lineage.get_source_for(_sanitize_name(d["table"])))),
            ("citation", d.get("citation", "")),
        ) if v}
        # Set outside the `if v` filter above so that False is shown for a dataset without one.
        entry["has_uncertainty_column"] = has_uncertainty_column(d["id"])
        datasets.append(entry)
    return {"datasets": datasets, "documented_use": DOCUMENTED_USE}


_YEAR_COL = re.compile(r"^y_(\d{4})$")


def _wide_years(names: list[str]) -> dict[str, Any] | None:
    """Return the count, first year, last year and names of the y_YYYY columns in `names`, or None if there are fewer than three."""
    years = sorted((int(m.group(1)), n) for n in names if (m := _YEAR_COL.match(n)))
    if len(years) < 3:
        return None
    return {"count": len(years), "first": years[0][0], "last": years[-1][0],
            "columns": [n for _, n in years]}


def list_columns(dataset_id: str, columns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A dataset's columns with its time column and, for a wide table, the year-series note.

    `columns` are the columns read live from the database; without them the catalogue's own list
    is used. The note is computed from whichever list is returned.
    """
    ds = find_dataset(dataset_id)
    if ds is None:
        return {"columns": [], "error": f"Dataset '{dataset_id}' not found. Use list_datasets to see available datasets."}
    cols = columns if columns is not None else ds["columns"]
    out: dict[str, Any] = {
        "dataset_id": ds["id"],
        "table": ds["table"],
        "columns": cols,
        "time_column": ds["time_column"],
        "error": None,
    }
    # A table with year columns gets a `next_step` text in the result, so the LLM reads that
    # the columns are a series to compare before it treats the column list as the answer.
    wide = _wide_years([c["name"] for c in cols])
    if wide:
        out["wide_years"] = wide
        out["next_step"] = (
            f"{wide['count']} of these columns are one value per YEAR, "
            f"{wide['columns'][0]} through {wide['columns'][-1]} "
            f"({wide['first']}-{wide['last']}). This table is a WIDE time series, "
            f"not a schema to describe: a trend, a decline or a change over time is "
            f"computed by comparing those columns. Use query_data to select "
            f"{wide['columns'][0]} and {wide['columns'][-1]} per row and rank the "
            f"difference, or aggregate over one of them for a single year. "
            f"Listing the column names is not an answer to a question about change "
            f"over time. The number is one call away."
        )
    return out


_UNCERTAINTY_MARKERS = (
    "uncertainty", "confidence", "_ci", "lcl", "ucl", "stderr", "std_err",
    "variance", "margin_of_error",
)


#: ICES publishes every estimate with low_ and high_ bounds, a confidence
#: interval; neither word is in the markers above, so they match as prefixes.
_UNCERTAINTY_PREFIXES = ("low_", "high_")


def uncertainty_columns(dataset_id: str) -> list[str]:
    """Return the names of the dataset's columns that hold an uncertainty measure, read from its catalogue column list.

    A column counts when its name contains one of `_UNCERTAINTY_MARKERS` or starts with `low_` or `high_`.
    """
    ds = find_dataset(dataset_id)
    if not ds:
        return []
    return [c["name"] for c in ds["columns"]
            if any(m in c["name"].lower() for m in _UNCERTAINTY_MARKERS)
            or c["name"].lower().startswith(_UNCERTAINTY_PREFIXES)]


def has_uncertainty_column(dataset_id: str) -> bool:
    """Return True if `uncertainty_columns` finds at least one column for the dataset."""
    return bool(uncertainty_columns(dataset_id))


def stock_columns(dataset_id: str) -> set[str]:
    """Measure columns whose additivity is 'stock' (semi-additive over time)."""
    ds = find_dataset(dataset_id)
    if not ds:
        return set()
    return {c["name"] for c in ds["columns"] if c.get("additivity") == "stock"}


def modelled_datasets() -> set[str]:
    """Return the ids of the datasets whose `kind` is "modelled"."""
    return {d["id"] for d in DATASETS if d.get("kind") == "modelled"}


def dataset_kind(dataset_id: str) -> str:
    """Return the dataset's `kind`, 'observed' or 'modelled', or '' when the dataset is unknown."""
    ds = find_dataset(dataset_id)
    return (ds or {}).get("kind", "")


def modelled_note(dataset_id: str) -> str:
    """Return the note that a number comes from a modelled table, or '' if the dataset is not modelled.

    The note says the calculation record covers the rows in the table and does not name a SURIMI run.
    """
    if dataset_kind(dataset_id) != "modelled":
        return ""
    return ("This number comes from a modelled table, not an observation. The "
            "receipt attests that the number was computed from the rows in that "
            "table; it does not attest that any SURIMI run produced them, and "
            "this server holds no provenance documentation for this dataset. "
            "Do not present it as measured data.")


def substitute_warning(dataset_ref: str) -> str:
    """Return the dataset's `not_a_substitute_for` text, or '' when it has none.

    `dataset_ref` can be a catalogue id, a table name or a Trino full name, because `query_data` takes whatever the caller typed.
    """
    ds = find_dataset(dataset_ref)
    if ds:
        return ds.get("not_a_substitute_for", "")
    ref = (dataset_ref or "").lower().replace(".", "_")
    for d in DATASETS:
        if not d.get("not_a_substitute_for"):
            continue
        tbl = d["table"].lower().replace(".", "_")
        if ref.endswith(tbl) or tbl.endswith(ref):
            return d["not_a_substitute_for"]
    return ""


def tables_in_sql(sql: str) -> list[str]:
    """Return the ids of the catalogue datasets whose table name appears in `sql`.

    It matches substrings of the registered table names and does not parse the SQL, so it can return extra ids.
    """
    low = (sql or "").lower().replace('"', "").replace("`", "")
    hits = []
    for d in DATASETS:
        t = d["table"].lower()
        if t in low or t.replace(".", "_") in low or t.split(".")[-1] in low:
            hits.append(d["id"])
    return hits
