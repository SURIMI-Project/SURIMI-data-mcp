"""SQL views over the cube's base tables, created when the cube is built.

Each view reads one slice of its base table, filtered to one `variable_code` where the table is EAV
(one row per variable) and aggregated, with gender and nationality values lower-cased and NULLs excluded.
The employment views have no `variable_code` column, so a query on them cannot sum across codes, and
`socemploy` (persons) and `socfte` (full-time equivalents) sit in separate views. The sales view
sums across markets.
"""
from __future__ import annotations

_SOCIAL = "hive_eu_dcf_aer_social_data_2017_2020"
_SALES = "hive_surimi_surimi_simulated_sales_output_aegean_sea"
_OECD = "hive_oecd_oecd_fisheries_support_estimates_eu_coastal_states"
# The raw-lake stem, without the "hive_eu_dcf_" prefix. It is the sanitized last segment of the
# catalogue table "hive.eu_dcf.aer_economic_national_level" (see tools.common.resolve_cube_dataset
# and metrics._cube_dataset).
_AER_NATIONAL = "aer_economic_national_level"

SEMANTIC_VIEWS: list[dict] = [
    {
        "name": "v_employment_by_gender",
        "base_table": _SOCIAL,
        "columns": ["gender", "employment_persons"],
        "requires": ["variable_code", "variable_value", "gender"],
        "description": "Persons employed (socemploy headcount) by gender, case-merged, NULL-gender excluded. NOT FTE.",
        "sql": f"""
            SELECT LOWER(gender) AS gender,
                   SUM(TRY_CAST(variable_value AS DOUBLE)) AS employment_persons
            FROM "{_SOCIAL}"
            WHERE variable_code = 'socemploy' AND gender IS NOT NULL
            GROUP BY LOWER(gender)
        """,
    },
    {
        "name": "v_employment_by_gender_year",
        "base_table": _SOCIAL,
        "columns": ["year", "gender", "employment_persons"],
        "requires": ["variable_code", "variable_value", "year", "gender"],
        "description": "Persons employed (socemploy) by year and gender, case-merged, NULL-gender excluded.",
        "sql": f"""
            SELECT year, LOWER(gender) AS gender,
                   SUM(TRY_CAST(variable_value AS DOUBLE)) AS employment_persons
            FROM "{_SOCIAL}"
            WHERE variable_code = 'socemploy' AND gender IS NOT NULL
            GROUP BY year, LOWER(gender)
        """,
    },
    {
        "name": "v_employment_by_nationality",
        "base_table": _SOCIAL,
        "columns": ["nationality", "employment_persons"],
        "requires": ["variable_code", "variable_value", "nationality"],
        "description": "Persons employed (socemploy) by nationality, NULL excluded.",
        "sql": f"""
            SELECT LOWER(nationality) AS nationality,
                   SUM(TRY_CAST(variable_value AS DOUBLE)) AS employment_persons
            FROM "{_SOCIAL}"
            WHERE variable_code = 'socemploy' AND nationality IS NOT NULL
            GROUP BY LOWER(nationality)
        """,
    },
    {
        "name": "v_fte_by_gender",
        "base_table": _SOCIAL,
        "columns": ["gender", "fte"],
        "requires": ["variable_code", "variable_value", "gender"],
        "description": "Full-time-equivalents (socfte) by gender, case-merged, NULL excluded. FTE, NOT a headcount.",
        "sql": f"""
            SELECT LOWER(gender) AS gender,
                   SUM(TRY_CAST(variable_value AS DOUBLE)) AS fte
            FROM "{_SOCIAL}"
            WHERE variable_code = 'socfte' AND gender IS NOT NULL
            GROUP BY LOWER(gender)
        """,
    },
    {
        "name": "v_sales_by_species_year",
        "base_table": _SALES,
        "columns": ["year", "species_code", "value_meur"],
        "requires": ["year", "species_code", "value_meur"],
        "description": "Simulated Aegean sales value (MEUR) by species and year, summed across markets.",
        "sql": f"""
            SELECT year, species_code,
                   ROUND(SUM(TRY_CAST(value_meur AS DOUBLE)), 2) AS value_meur
            FROM "{_SALES}"
            GROUP BY year, species_code
        """,
    },
    {
        # aer-economic-national-level is EAV (one row per variable_code), so the three variables
        # are pivoted into columns here. The table has one row per (year, country, code), and MAX
        # takes that row's value, where SUM would double an accidental duplicate.
        "name": "v_fuel_price_by_country_year",
        "base_table": _AER_NATIONAL,
        # The same table has two names depending on the ingest path: the raw-lake stem when the
        # cube is built from the lake, and the hive_-prefixed name when it is built from
        # data/exports. apply_semantic_views uses whichever one exists.
        "base_tables": [_AER_NATIONAL, "hive_eu_dcf_" + _AER_NATIONAL],
        "columns": ["year", "country_code", "country_name",
                    "totenercost", "totenercons", "totlandginc",
                    "implied_fuel_price", "energy_cost_share"],
        "requires": ["year", "country_code", "country_name", "variable_code", "value"],
        "description": (
            "Energy cost (EUR), energy consumption (litres) and landings "
            "income (EUR) by country and year, pivoted from the AER "
            "national-level EAV table, plus the two ratios derived from "
            "them. The aggregate engine's measures must be real columns "
            "(no arithmetic expression), so the ratios are computed here, "
            "not in metrics.py."
        ),
        # The `aggregate` measures must be real columns (_require_column in aggregate.py rejects
        # anything else), so the two ratios are calculated here and not as a metric measure expression.
        "sql": f"""
            SELECT year, country_code, country_name,
                   MAX(CASE WHEN variable_code = 'totenercost' THEN TRY_CAST(value AS DOUBLE) END) AS totenercost,
                   MAX(CASE WHEN variable_code = 'totenercons' THEN TRY_CAST(value AS DOUBLE) END) AS totenercons,
                   MAX(CASE WHEN variable_code = 'totlandginc' THEN TRY_CAST(value AS DOUBLE) END) AS totlandginc,
                   MAX(CASE WHEN variable_code = 'totenercost' THEN TRY_CAST(value AS DOUBLE) END)
                       / MAX(CASE WHEN variable_code = 'totenercons' THEN TRY_CAST(value AS DOUBLE) END) AS implied_fuel_price,
                   MAX(CASE WHEN variable_code = 'totenercost' THEN TRY_CAST(value AS DOUBLE) END)
                       / MAX(CASE WHEN variable_code = 'totlandginc' THEN TRY_CAST(value AS DOUBLE) END) AS energy_cost_share
            FROM "{_AER_NATIONAL}"
            WHERE variable_code IN ('totenercost', 'totenercons', 'totlandginc')
            GROUP BY year, country_code, country_name
        """,
    },
    {
        "name": "v_subsidies_by_country_year",
        "base_table": _OECD,
        "columns": ["year", "country_code", "direct_transfers_meur",
                    "cost_reducing_transfers_meur", "general_services_meur", "total_support_meur"],
        "requires": ["year", "country_code", "direct_transfers_meur",
                     "cost_reducing_transfers_meur", "general_services_meur"],
        "description": "OECD fisheries support (MEUR) by country and year, with total = direct + cost-reducing + general services.",
        # There is no GROUP BY below, which assumes the base table has one row per (country_code, year).
        "sql": f"""
            SELECT year, country_code,
                   TRY_CAST(direct_transfers_meur AS DOUBLE) AS direct_transfers_meur,
                   TRY_CAST(cost_reducing_transfers_meur AS DOUBLE) AS cost_reducing_transfers_meur,
                   TRY_CAST(general_services_meur AS DOUBLE) AS general_services_meur,
                   ROUND(TRY_CAST(direct_transfers_meur AS DOUBLE)
                       + TRY_CAST(cost_reducing_transfers_meur AS DOUBLE)
                       + TRY_CAST(general_services_meur AS DOUBLE), 2) AS total_support_meur
            FROM "{_OECD}"
        """,
    },
]


def _existing_tables(con) -> set:
    """Return the names of the tables in the connection's `main` schema."""
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()
    return {r[0] for r in rows}


def _table_columns(con, table_name: str) -> set:
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='main' AND table_name = ?", [table_name]).fetchall()
    return {r[0] for r in rows}


def apply_semantic_views(con) -> list[str]:
    """Create or replace each view whose base table exists and has every column in its `requires` list.

    It returns the names of the views it created. A view whose base table or required column is missing is skipped,
    because creating it would raise a DuckDB binder error and stop the loop.
    """
    have = _existing_tables(con)
    made: list[str] = []
    for v in SEMANTIC_VIEWS:
        # A view can list several names for its base table, one per ingest path.
        candidates = v.get("base_tables") or [v["base_table"]]
        base = next((t for t in candidates if t in have), None)
        if base is None:
            continue  # base table absent in this cube (e.g. partial fixture) -> skip
        base_columns = _table_columns(con, base)
        missing = [c for c in v["requires"] if c not in base_columns]
        if missing:
            continue  # base table present but missing required column(s) -> skip
        sql = v["sql"]
        if base != v["base_table"]:
            sql = sql.replace(f'"{v["base_table"]}"', f'"{base}"')
        con.execute(f'CREATE OR REPLACE VIEW "{v["name"]}" AS {sql}')
        made.append(v["name"])
    return made
