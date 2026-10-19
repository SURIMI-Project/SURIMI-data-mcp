"""Lookups behind the explanation tools: indicator definitions, species codes and EwE functional groups."""
from __future__ import annotations

from typing import Any, Optional

from surimi_data_mcp.datasets.catalog import DATASETS
from surimi_data_mcp.paths import APP_ROOT


_INDICATOR_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "direct_transfers_meur": {
        "definition": "Direct government transfers to the fisheries sector, measured in millions of EUR.",
        "unit": "million EUR",
        "source": "OECD Fisheries Support Estimates",
    },
    "cost_reducing_transfers_meur": {
        "definition": "Government transfers that reduce the cost of fisheries inputs (fuel, gear, etc.), in millions of EUR.",
        "unit": "million EUR",
        "source": "OECD Fisheries Support Estimates",
    },
    "general_services_meur": {
        "definition": "Government expenditure on general services benefiting the fisheries sector (research, management, enforcement), in millions of EUR.",
        "unit": "million EUR",
        "source": "OECD Fisheries Support Estimates",
    },
    "employment_fte": {
        "definition": "Employment in the fisheries sector measured in full-time equivalents.",
        "unit": "FTE",
        "source": "EU Data Collection Framework (DCF)",
    },
    "gross_revenue_meur": {
        "definition": "Total gross revenue from fisheries operations, in millions of EUR.",
        "unit": "million EUR",
        "source": "EU DCF Economic Performance",
    },
    "operating_cost_meur": {
        "definition": "Total operating costs of fisheries operations, in millions of EUR.",
        "unit": "million EUR",
        "source": "EU DCF Economic Performance",
    },
    "gross_value_added_meur": {
        "definition": "Gross value added by the fisheries sector (revenue minus intermediate consumption), in millions of EUR.",
        "unit": "million EUR",
        "source": "EU DCF Economic Performance",
    },
    "profit_margin": {
        "definition": "Net profit as a percentage of revenue for fisheries operations.",
        "unit": "ratio (0-1)",
        "source": "EU DCF Economic Performance",
    },
    "gender_balance_ratio": {
        "definition": "Ratio of female to total employment in the fisheries sector.",
        "unit": "ratio (0-1)",
        "source": "EU DCF Employment Data",
    },
    "biomass_kt": {
        "definition": "Estimated fish stock biomass from survey data, in thousands of tonnes.",
        "unit": "kilotonnes (kt)",
        "source": "SURIMI Empirical Biomass Surveys",
    },
    "cv_uncertainty": {
        "definition": "Coefficient of variation measuring uncertainty in the biomass estimate.",
        "unit": "ratio",
        "source": "SURIMI Empirical Biomass Surveys",
    },
    "quantity_kt": {
        "definition": "Quantity of fish sold, in thousands of tonnes.",
        "unit": "kilotonnes (kt)",
        "source": "SURIMI Simulated Sales",
    },
    "price_eur_per_kg": {
        "definition": "Average fish sale price in EUR per kilogram.",
        "unit": "EUR/kg",
        "source": "SURIMI Simulated Sales",
    },
    "value_meur": {
        "definition": "Total sales value in millions of EUR.",
        "unit": "million EUR",
        "source": "SURIMI Simulated Sales",
    },
    "landings": {
        "definition": "Nominal fisheries landings (catch brought ashore), per country, species and area or division.",
        "unit": "tonnes (live weight)",
        "source": "DataGUI processed FAO Area 37 and ICES Area 27 landings",
    },
    "fuel_efficiency": {
        "definition": "Ratio of fuel cost to total revenue, indicating fuel dependency.",
        "unit": "ratio",
        "source": "EU DCF Fuel Costs",
    },
}


def explain_indicator(
    indicator: str,
    dataset_id: Optional[str] = None,
) -> dict[str, Any]:
    """Return what the server holds about an indicator name, as a dict with `found`, `definition`, `unit`, `source` and `dataset_id`.

    `found` is True only for an entry in `_INDICATOR_DESCRIPTIONS`. A code read from the data, or a column with no description, returns `found` False with a `note` and the dataset it was seen in.
    """
    indicator_lower = indicator.lower().strip()

    if indicator_lower in _INDICATOR_DESCRIPTIONS:
        info = _INDICATOR_DESCRIPTIONS[indicator_lower]
        ds_id = _find_dataset_for_indicator(indicator_lower, dataset_id)
        return {
            "indicator": indicator,
            "found": True,
            "definition": info["definition"],
            "unit": info["unit"],
            "source": info["source"],
            "dataset_id": ds_id,
        }

    eav = _eav_vocabulary().get(indicator_lower)
    if eav:
        # The label of a code is not a definition. "totdirsub" gives "Operating
        # subsidies (Income)", which names the line of the accounts and does not say
        # what an operating subsidy is. The result carries the label, unit and table,
        # and `found` stays False because the server holds no definition.
        return {
            "indicator": indicator,
            "found": False,
            "definition": None,
            "label": eav["definition"],
            "unit": eav["unit"],
            "source": eav["source"],
            "dataset_id": eav["dataset_id"],
            "column_exists_in": eav["dataset_id"],
            "note": (f"'{indicator}' is recorded in {eav['dataset_id']} as "
                     f"\"{eav['definition']}\", measured in {eav['unit']}. That is "
                     f"the label it is filed under, not a definition: this server "
                     f"holds no definition of the term itself."),
        }

    # A column without a description is not an explained indicator, so `found` stays
    # False and the dataset that holds the column is reported separately.
    ds_id = _find_dataset_for_indicator(indicator_lower, dataset_id)
    if ds_id:
        return {
            "indicator": indicator,
            "found": False,
            "definition": None,
            "unit": None,
            "source": None,
            "dataset_id": ds_id,
            "column_exists_in": ds_id,
            "note": (f"'{indicator}' is a column in dataset '{ds_id}', but this "
                     f"server holds no definition for it. Its meaning is not "
                     f"established here."),
        }

    return {
        "indicator": indicator,
        "found": False,
        "definition": None,
        "unit": None,
        "source": None,
        "dataset_id": None,
    }


# EAV tables carry their own vocabulary in the rows: a variable code, its label and
# its unit. Each tuple is (dataset id, table, code column, label column, unit column,
# group column).
_EAV_SOURCES = (
    ("aer-economic-national-level", "hive_eu_dcf_aer_economic_national_level",
     "variable_code", "variable_name", "unit", "variable_group"),
)

_EAV_CACHE: Optional[dict[str, dict[str, Any]]] = None


def _eav_vocabulary() -> dict[str, dict[str, Any]]:
    """Return a mapping from lower-case code to `definition`, `unit`, `source` and `dataset_id`, read from the cube.

    The server caches a successful read. When the cube is unavailable the function returns an empty mapping without caching it, so the next call tries again.
    """
    global _EAV_CACHE
    if _EAV_CACHE is not None:
        return _EAV_CACHE
    try:
        out = _read_eav_vocabulary()
    except Exception:
        return {}                      # transient: retry on the next call
    _EAV_CACHE = out
    return out


def _read_eav_vocabulary() -> dict[str, dict[str, Any]]:
    """Read the codes, labels and units of the `_EAV_SOURCES` tables from the cube, and raise when the cube is unavailable."""
    out: dict[str, dict[str, Any]] = {}
    if True:
        from surimi_data_mcp.engine import cube as cube_module
        from surimi_data_mcp.engine.aggregate import _q
        cube = cube_module.get_cube()
        available = set(cube.datasets())
        for ds_id, table, code_col, name_col, unit_col, group_col in _EAV_SOURCES:
            if table not in available:
                continue
            rows = cube.query(
                f"SELECT DISTINCT {_q(code_col)} AS code, {_q(name_col)} AS label, "
                f"{_q(unit_col)} AS unit, {_q(group_col)} AS grp "
                f"FROM {_q(table)} WHERE {_q(code_col)} IS NOT NULL")
            for r in rows:
                code = str(r["code"]).strip()
                if not code:
                    continue
                label = str(r["label"] or "").strip()
                grp = str(r["grp"] or "").strip()
                definition = label or code
                if grp:
                    definition = f"{definition} ({grp})"
                out.setdefault(code.lower(), {
                    "definition": definition,
                    "unit": str(r["unit"] or "unknown").strip() or "unknown",
                    "source": "EU Data Collection Framework (DCF), as coded in the data",
                    "dataset_id": ds_id,
                })
    return out


def _find_dataset_for_indicator(
    indicator: str,
    preferred_dataset_id: Optional[str] = None,
) -> Optional[str]:
    """Return the id of the first dataset with a column named `indicator`, trying `preferred_dataset_id` first, or None."""
    if preferred_dataset_id:
        for ds in DATASETS:
            if ds["id"] == preferred_dataset_id:
                if any(c["name"] == indicator for c in ds["columns"]):
                    return ds["id"]

    for ds in DATASETS:
        if any(c["name"] == indicator for c in ds["columns"]):
            return ds["id"]

    return None


# Species vocabulary files, tried in order, each relative to APP_ROOT or its parent.
# The first copy ships with this repository. The two `references/` copies exist only
# in a monorepo checkout.
#
# A code is not a unique key. species.csv has 90 rows under 86 distinct codes, and
# ANE, HKE, MUT and PIL each appear twice, split by life stage. The lookup returns
# every matching row.
_SPECIES_FILES = (
    "data/vocabularies/species.csv",
    "references/SURIMI-western_med/SURIMI-market_inputs/species.csv",
    "references/SURIMI-market/config/species.csv",
)

_SPECIES_CACHE: Optional[tuple[dict[str, list[dict[str, str]]], str]] = None


def _species_vocabulary() -> tuple[dict[str, list[dict[str, str]]], str]:
    """Return a mapping from lower-case code to rows of `life_stage`, `species_name` and `scientific_name`, with the path of the file read.

    The mapping is empty when no vocabulary file can be read.
    """
    global _SPECIES_CACHE
    if _SPECIES_CACHE is not None:
        return _SPECIES_CACHE
    import csv
    import os

    roots = (str(APP_ROOT), str(APP_ROOT.parent))
    out: dict[str, list[dict[str, str]]] = {}
    used = ""
    for rel in _SPECIES_FILES:
        path = next((os.path.join(r, rel) for r in roots
                     if os.path.exists(os.path.join(r, rel))), None)
        if path is None:
            continue
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    code = str(row.get("species_code") or "").strip()
                    name = str(row.get("species_name") or "").strip()
                    if not code or not name:
                        continue
                    stage = str(row.get("life_stage") or "").strip()
                    out.setdefault(code.lower(), []).append({
                        "life_stage": "" if stage.upper() == "NA" else stage,
                        "species_name": name,
                        "scientific_name": _scientific_name(name),
                    })
            used = rel
            break
        except Exception:
            continue
    _SPECIES_CACHE = (out, used)
    return _SPECIES_CACHE


def _scientific_name(species_name: str) -> str:
    """Remove the parenthesised common name from a species name, so "Sardina pilchardus (...)" gives "Sardina pilchardus"."""
    return species_name.split("(")[0].strip() or species_name


# The case-study vocabulary covers 86 codes. The landings table has many more
# alpha3_code values, each with a common_name. `explain_species_code` reads the
# vocabulary first because it carries the scientific name and the life-stage
# split, and falls back to the table for any other code.
_SPECIES_TABLE = "hive.fisheries.fisheries_landings"

_SPECIES_TABLE_CACHE: Optional[dict[str, str]] = None


def _species_table_client():
    """Return the `execute(sql, limit=...)` method of the shared database client, which reads the landings table.

    The import sits inside the function, so it runs only when a lookup needs the database. Tests replace this function.
    """
    from surimi_data_mcp.tools.common import get_trino
    return get_trino().execute


def _species_table_lookup(code: str) -> Optional[dict[str, str]]:
    """Return `common_name` and `scientific_name` for an `alpha3_code` in the landings table, or None.

    The server reads the distinct codes and names once through the database client, up to 5000 rows, and caches them. A failed or empty read is not cached, so the next call tries again. `scient_name` can be empty, and the common name is then the only name returned.
    """
    global _SPECIES_TABLE_CACHE
    if _SPECIES_TABLE_CACHE is None:
        try:
            execute = _species_table_client()
            res = execute(
                f"SELECT DISTINCT alpha3_code, common_name, scient_name "
                f"FROM {_SPECIES_TABLE} "
                f"WHERE alpha3_code IS NOT NULL AND common_name IS NOT NULL",
                limit=5000)
        except Exception:
            return None                # transient: retry on the next call
        out: dict[str, dict[str, str]] = {}
        for r in res.get("rows", []):
            c = str(r.get("alpha3_code") or "").strip()
            n = str(r.get("common_name") or "").strip()
            sci = str(r.get("scient_name") or "").strip()
            if c and n:
                out.setdefault(c.lower(), {"common_name": n,
                                           "scientific_name": sci})
        if not out:
            return None                # nothing read: do not pin an empty map
        _SPECIES_TABLE_CACHE = out
    return _SPECIES_TABLE_CACHE.get(str(code).lower().strip())


def explain_species_code(code: str) -> dict[str, Any]:
    """Return the species name recorded for a three-letter code, looked up in the project vocabulary and then in the landings table.

    A code with several vocabulary rows returns all of them in `matches`, and `scientific_name` is None when the rows name different organisms.
    """
    vocab, source_file = _species_vocabulary()
    matches = vocab.get(str(code).lower().strip(), [])
    if not matches:
        # Not a case-study species: try the landings table.
        hit = _species_table_lookup(code)
        if hit:
            common = hit["common_name"]
            sci = hit.get("scientific_name") or ""
            note = (f"'{code}' is {common}" + (f" ({sci})" if sci else "") +
                    ", as recorded in the fisheries landings table. It is not "
                    "a western-Med case-study species, so the life-stage split "
                    "the case study carries does not apply.")
            if not sci:
                note += " That row records no scientific name."
            return {
                "code": code,
                "found": True,
                "scientific_name": sci or None,
                "common_name": common,
                "matches": [{"life_stage": "", "species_name": common,
                             "scientific_name": sci}],
                "source": (f"{_SPECIES_TABLE} "
                           f"(alpha3_code -> common_name, scient_name)"),
                "note": note,
            }
        return {
            "code": code,
            "found": False,
            "scientific_name": None,
            "matches": [],
            "source": source_file or "no species vocabulary file was reachable",
            "note": (f"'{code}' is in neither the SURIMI species vocabulary nor "
                     f"the fisheries landings table. This server does not hold "
                     f"a name for it."),
        }

    names = {m["scientific_name"] for m in matches}
    scientific = sorted(names)[0] if len(names) == 1 else None
    stages = [m["life_stage"] for m in matches if m["life_stage"]]

    if len(names) > 1:
        # Same code, different organisms: the note says so and no name is chosen.
        note = (f"'{code}' is ambiguous in {source_file}: it maps to "
                f"{len(matches)} entries with different scientific names "
                f"({', '.join(sorted(names))}). This server cannot tell which "
                f"one a question means.")
    elif stages:
        note = (f"'{code}' is {scientific}. It appears {len(matches)} times in "
                f"{source_file}, once per life stage ({', '.join(stages)}); the "
                f"species is the same for each, so any per-stage figure must "
                f"name the stage it used.")
    else:
        note = f"'{code}' is {scientific}, recorded in {source_file}."

    return {
        "code": code,
        "found": True,
        "scientific_name": scientific,
        "matches": matches,
        "source": source_file,
        "note": note,
    }


# The file lists 59 Ecopath functional groups in 111 rows and, for the groups that
# have them, their member species. It ships with this repository like species.csv.
_EWE_GROUPS_FILE = "data/vocabularies/ewe_functional_groups.csv"

_EWE_GROUPS_CACHE: Optional[list[dict[str, str]]] = None


def _ewe_groups() -> list[dict[str, str]]:
    """Return every row of the EwE functional-group table as read from the file, with values stripped of spaces, and cache it."""
    global _EWE_GROUPS_CACHE
    if _EWE_GROUPS_CACHE is not None:
        return _EWE_GROUPS_CACHE
    import csv
    import os

    path = os.path.join(str(APP_ROOT), _EWE_GROUPS_FILE)
    rows: list[dict[str, str]] = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                rows.append({k: (v or "").strip() for k, v in row.items()})
    _EWE_GROUPS_CACHE = rows
    return _EWE_GROUPS_CACHE


def list_functional_groups(group: Optional[str] = None) -> dict[str, Any]:
    """Return the member species of every Ecopath functional group whose name contains `group`.

    With no `group`, the result lists every distinct `EwE_Group_Name`. The match ignores case, and the source file spells one group name "Small demersal fishesl (continental shelf)", which the result repeats unchanged. A matching group with no species rows, such as Phytoplankton, returns `found` True with an empty species list.
    """
    rows = _ewe_groups()
    if not rows:
        return {"query": group, "found": False, "groups": [], "species": [],
                "n_species": 0, "source": _EWE_GROUPS_FILE,
                "note": f"{_EWE_GROUPS_FILE} was not reachable"}

    if group is None:
        names = sorted({r["EwE_Group_Name"] for r in rows})
        return {"query": None, "found": True, "groups": names,
                "n_groups": len(names), "source": _EWE_GROUPS_FILE}

    needle = group.strip().lower()
    matches = [r for r in rows if needle in r["EwE_Group_Name"].lower()]
    if not matches:
        return {"query": group, "found": False, "matched_group_names": [],
                "species": [], "n_species": 0, "source": _EWE_GROUPS_FILE,
                "note": f"no functional group name contains {group!r}"}

    matched_group_names = sorted({r["EwE_Group_Name"] for r in matches})
    species = [
        {
            "scientific_name": f"{r['Genus']} {r['Species']}".strip(),
            "common_name": r["Common_Name"],
            "fao_code": r["FAO_Code"],
            "is_fished": r["Is_fished"],
        }
        for r in matches if r.get("Species")
    ]
    out = {
        "query": group,
        "found": True,
        "matched_group_names": matched_group_names,
        "species": species,
        "n_species": len(species),
        "source": _EWE_GROUPS_FILE,
    }
    series = next((s for key, s in _GROUP_MODEL_SERIES.items()
                   if any(key in n.lower() for n in matched_group_names)), None)
    if series:
        out["model_series"] = series
        out["survey_note"] = _SURVEY_NOTE
    return out


#: Functional groups whose biomass is available as a gridded series on the model
#: server. `list_functional_groups` adds the matching entry to its result.
_GROUP_MODEL_SERIES: dict[str, dict[str, Any]] = {
    "small demersal": {
        "server": "model",
        "variable": "bd30cm",
        "long_name": "Biomass density of small demersals under 30 cm",
        "units": "g m-2",
        "coverage": "monthly, 1961-01 to 2009-12 with values (2010-2014 empty)",
        "origin": ("EcoOcean, external FishMIP/ISIMIP3a output (control climate, "
                   "historical fishing), not a SURIMI run"),
        "tools": ["model_time_series", "model_trend", "model_compare_periods"],
        "how": ("On the model server, find the bd30cm file with model_list_files, "
                "then call model_time_series or model_trend with region "
                "'Mediterranean' for a biomass level or trend."),
    },
}

_SURVEY_NOTE = (
    "MEDITS (surimi-biomass-medits on this server) gives bottom-trawl survey CPUE "
    "for the member species: a catch-rate abundance index, not biomass. For a "
    "biomass figure use model_series. Say which of the two an answer uses."
)
