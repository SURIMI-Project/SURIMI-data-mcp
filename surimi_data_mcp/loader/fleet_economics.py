"""Derive EU DCF fleet economics from the AER source, one row per fleet segment and year.

The AUTH indicator file behind `eu_dcf.rev_grp` is in long (EAV) form, with one row per
AER variable, so a single row of it is not a fleet segment and `SUM`, `AVG` or `COUNT`
over its columns answer the wrong question. This module reads the AER source file
that AUTH's file was built from and writes its own wide table, with one row per segment
and year and one column per measure. The AUTH file in the lake stays untouched.

Definitions follow the STECF Annual Economic Report on the EU Fishing Fleet 2024
(STECF-24-03 and STECF-24-07), https://data.europa.eu/doi/10.2760/5037826:

    total_income = landings_value + other_income + rights_income
                   + operating_subsidies
    gva          = total_income - energy_cost - repair_cost
                   - other_variable_cost - other_nonvariable_cost - rights_cost
    gross_profit = gva - personnel_cost - unpaid_labour_value
    net_profit   = gross_profit - depreciation

``net_profit`` is before the opportunity cost of capital, which the AER extract does not
carry. ``gross_profit_excl_subsidies_and_rights`` uses only landings and other income as
the income base, which is the basis of AUTH's Rev_GRP indicator. `scripts/build_fleet_economics.py`
compares it with that indicator.

Clusters and subsegments
------------------------
AER reports some segments at cluster level. Every member of such a cluster carries the
same value under a shared ``fs_name`` that ends in ``*``, and only ``cluster_name``
differs. Other segments are split by activity (A for active, L for low-active) and carry
different values under different ``fs_name`` values. The code takes one value per
``(segment key, fs_name, variable_code)`` and sums those values across ``fs_name``, so a
cluster is counted once and the activity subsegments are added.

Missing values
--------------
AER writes ``NA`` where a Member State reported nothing and ``0`` where it reported a
measured zero. A component column keeps ``NA`` as ``None``. The derived columns treat a
missing component as zero, except for a segment-year with no component at all, whose
derived columns are all ``None``.
"""
from __future__ import annotations

import csv
from typing import Any, Iterable

# The key of a fleet segment-year. ``activity`` and ``cluster_name`` are not part of it,
# so their rows are combined as described in the module docstring.
DIMENSIONS: tuple[str, ...] = (
    "country_name",
    "country_code",
    "year",
    "supra_reg",
    "fishing_tech",
    "vessel_length",
    "geo_indicator",
)

# AER ``variable_code`` -> column name. Variable codes not listed here are ignored.
AER_MEASURES: dict[str, str] = {
    # Income
    "totlandginc": "landings_value",
    "tototherinc": "other_income",
    "totrightsinc": "rights_income",
    "totdirsub": "operating_subsidies",
    # Expenditure
    "totenercost": "energy_cost",
    "totrepcost": "repair_cost",
    "totvarcost": "other_variable_cost",
    "totnovarcost": "other_nonvariable_cost",
    "totrightscost": "rights_cost",
    "totcrewwage": "personnel_cost",
    "totunpaidlab": "unpaid_labour_value",
    "totdepcost": "depreciation",
}

_INCOME = ("landings_value", "other_income", "rights_income", "operating_subsidies")
# The income base of AUTH's Rev_GRP indicator.
_NARROW_INCOME = ("landings_value", "other_income")
_OPERATING_COST = (
    "energy_cost",
    "repair_cost",
    "other_variable_cost",
    "other_nonvariable_cost",
    "rights_cost",
)
_LABOUR = ("personnel_cost", "unpaid_labour_value")

# Columns calculated from the measures, in output order.
DERIVED: tuple[str, ...] = (
    "total_income",
    "gva",
    "gross_profit",
    "gross_profit_excl_subsidies_and_rights",
    "net_profit",
)

COLUMNS: tuple[str, ...] = DIMENSIONS + tuple(AER_MEASURES.values()) + DERIVED


def read_aer_rows(path: str) -> list[dict[str, str]]:
    """Read the AER economic CSV at ``path`` into a list of rows of strings, without converting any value."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _number(raw: str | None) -> float | None:
    """Parse an AER number. ``NA``, an empty string and any text that is not a number give ``None``."""
    if raw is None:
        return None
    try:
        return float(raw.strip())
    except (ValueError, AttributeError):
        return None


def _year(raw: str) -> int | str:
    """Return the year as an int, or the original text when it is not a whole number."""
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return raw


def derive_fleet_economics(rows: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    """Pivot AER rows into one wide row per fleet segment and year, in the order of `COLUMNS`.

    A component is the sum of its values across ``fs_name`` and is ``None`` when no row for it carried a number.
    """
    # One value per (segment key, fs_name, column): a clustered value is counted
    # once and each activity subsegment is counted on its own.
    reported: dict[tuple, float] = {}
    order: list[tuple] = []
    keys: set[tuple] = set()

    for row in rows:
        column = AER_MEASURES.get(row.get("variable_code"))
        if column is None:
            continue
        key = tuple(row.get(d, "") for d in DIMENSIONS)
        if key not in keys:
            keys.add(key)
            order.append(key)
        value = _number(row.get("value"))
        if value is None:
            continue
        reported[(key, row.get("fs_name", ""), column)] = value

    segments: dict[tuple, dict[str, float | None]] = {
        k: {c: None for c in AER_MEASURES.values()} for k in order
    }
    for (key, _fs, column), value in reported.items():
        current = segments[key][column]
        segments[key][column] = value if current is None else current + value

    out: list[dict[str, Any]] = []
    for key in sorted(order):
        parts = segments[key]

        def total(names: tuple[str, ...]) -> float:
            """Sum the named components, counting a missing one as zero."""
            return sum(parts[n] or 0.0 for n in names)

        # With no component reported at all, the derived columns stay None instead of 0.
        if all(v is None for v in parts.values()):
            income = gva = gross_profit = narrow = net = None
        else:
            income = total(_INCOME)
            gva = income - total(_OPERATING_COST)
            gross_profit = gva - total(_LABOUR)
            narrow = total(_NARROW_INCOME) - total(_OPERATING_COST) - total(_LABOUR)
            net = gross_profit - (parts["depreciation"] or 0.0)

        record: dict[str, Any] = dict(zip(DIMENSIONS, key))
        record["year"] = _year(record["year"])
        record.update(parts)
        record["total_income"] = income
        record["gva"] = gva
        record["gross_profit"] = gross_profit
        record["gross_profit_excl_subsidies_and_rights"] = narrow
        record["net_profit"] = net
        out.append({c: record[c] for c in COLUMNS})

    return out


def write_csv(rows: list[dict[str, Any]], path: str) -> int:
    """Write the rows to a CSV file at ``path`` in the order of `COLUMNS` and return the number of rows written. `None` is written as an empty field."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({c: ("" if row[c] is None else row[c]) for c in COLUMNS})
    return len(rows)
