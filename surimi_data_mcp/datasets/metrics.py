"""Semantic metrics layer over the DuckDB cube.

A `Metric` names one quantity and holds what the server needs to calculate it: the dataset, the
measure column or expression, the aggregation, whether it is a flow, stock or ratio over time, the
unit, the dimensions it can be sliced by and, for an EAV table, the `variable_code` it binds. The LLM
picks a metric and slices it through `query_metric`, so it never chooses the table or the aggregation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Optional, Sequence

from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.engine.aggregate import (Cube, aggregate as _aggregate,
                       apply_latest_year_default, _MAGNITUDE_FNS,
                       _DEFAULT_EAV_CODE_COL, _time_span, _q)

_ADDITIVITY = {"flow", "stock", "ratio"}
_AGGS = {"sum", "avg", "count", "min", "max", "count_distinct"}


class MetricError(ValueError):
    """Raised for an invalid metric definition or an invalid `query_metric` request."""


@dataclass(frozen=True)
class Metric:
    """One named quantity: its dataset, measure, aggregation, additivity, unit and the dimensions it can be sliced by."""

    name: str
    label: str
    dataset: str                 # catalog id
    measure: str                 # real column or arithmetic expr over real columns
    agg: str
    additivity: str
    unit: str
    dimensions: tuple[str, ...]
    time: str = "year"
    eav_code: Optional[str] = None
    # Filters the metric always applies, as (column, values): the rows it counts whatever the
    # caller asks for. female_employment binds gender to Female this way.
    fixed: tuple[tuple[str, tuple[str, ...]], ...] = ()
    description: str = ""
    # "observed" or "modelled". "" means not set, and run_query_metric raises on it.
    origin: str = ""


def _cube_dataset(metric: Metric, cube: Cube) -> str:
    """Return the cube's dataset name for the metric's catalogue dataset, or raise MetricError if the cube has none."""
    from surimi_data_mcp.engine.cube import _sanitize_name
    names = set(cube.datasets())
    ds = catalog.find_dataset(metric.dataset)
    if ds:
        table = ds["table"]
        # The cube keys datasets by the sanitized file stem (dots become underscores), for example
        # 'hive_eu_dcf_aer_capacity' for table 'hive.eu_dcf.aer_capacity', so that form is tried too.
        for cand in (table, table.split(".")[-1], _sanitize_name(table),
                     _sanitize_name(metric.dataset), metric.dataset):
            if cand in names:
                return cand
        raise MetricError(f"metric {metric.name!r}: dataset {metric.dataset!r} not in cube")
    # A dataset that is not in the catalogue, such as a test cube, is tried by its id.
    if metric.dataset in names:
        return metric.dataset
    raise MetricError(f"metric {metric.name!r}: unknown dataset {metric.dataset!r}")


def validate_registry(registry: Sequence[Metric], cube: Cube) -> None:
    """Raise MetricError unless every metric is coherent and resolves against the cube's columns.

    It checks for duplicate names, a valid additivity and aggregation, and that the measure, dimensions, `eav_code` and fixed filters all name real columns.
    """
    seen: set[str] = set()
    for m in registry:
        if m.name in seen:
            raise MetricError(f"duplicate metric name {m.name!r}")
        seen.add(m.name)
        if m.additivity not in _ADDITIVITY:
            raise MetricError(f"{m.name}: bad additivity {m.additivity!r}")
        if m.agg not in _AGGS:
            raise MetricError(f"{m.name}: bad agg {m.agg!r}")
        if m.additivity == "ratio" and m.agg in _MAGNITUDE_FNS - {"avg", "min", "max"}:
            raise MetricError(f"{m.name}: ratio metric cannot use agg={m.agg!r}")
        cube_ds = _cube_dataset(m, cube)
        cols = {c["name"] for c in cube.columns(cube_ds)}
        # measure: a bare column must exist; an expression must reference only real cols
        tokens = _measure_tokens(m.measure)
        unknown = [t for t in tokens if t not in cols]
        if unknown:
            raise MetricError(f"{m.name}: measure references unknown column(s) {unknown}")
        for d in m.dimensions:
            if d not in cols:
                raise MetricError(f"{m.name}: dimension {d!r} not a column of {m.dataset}")
        if m.eav_code is not None and _DEFAULT_EAV_CODE_COL not in cols:
            raise MetricError(f"{m.name}: eav_code set but no {_DEFAULT_EAV_CODE_COL} column")
        for col, _values in m.fixed:
            if col not in cols:
                raise MetricError(f"{m.name}: fixed filter column {col!r} not a column of {m.dataset}")


def _measure_tokens(measure: str) -> list[str]:
    """Return the column names used in a measure, which is a column or an arithmetic expression over columns.

    Only letters, digits, underscores, `+ - * / ( )` and whitespace are accepted. Any other character raises MetricError.
    """
    if not re.fullmatch(r"[A-Za-z0-9_+\-*/()\s]+", measure):
        raise MetricError(f"measure expression has illegal characters: {measure!r}")
    return [t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", measure)]


# ───────────────────────── the registry ─────────────────────────
# `origin` is not set on the entries below. REGISTRY copies it from each dataset's `kind`, and a
# metric whose dataset id is not in the catalogue ends up with an empty origin.
_REGISTRY_PRE: tuple[Metric, ...] = (
    Metric("vessel_count", "Number of fishing vessels", "aer-capacity",
           "number_of_vessels", "sum", "stock", "vessels",
           ("country_name", "country_code", "fishing_tech", "vessel_length", "gear", "supra_reg"),
           description="Fleet size; per-year snapshot (defaults to the latest year)."),
    Metric("vessel_power", "Total engine power", "aer-capacity",
           "total_vessel_power", "sum", "stock", "kW",
           ("country_name", "country_code", "fishing_tech", "vessel_length")),
    Metric("vessel_tonnage", "Total tonnage", "aer-capacity",
           "total_vessel_tonnage", "sum", "stock", "GT",
           ("country_name", "country_code", "fishing_tech", "vessel_length")),
    # These five metrics read eu-dcf-fleet-economics, which has one column per quantity
    # (see loader/fleet_economics.py).
    Metric("fleet_revenue", "Fishing-fleet revenue (gross value of landings)",
           "eu-dcf-fleet-economics", "landings_value", "sum", "flow", "EUR",
           ("country_name", "country_code", "supra_reg", "fishing_tech",
            "vessel_length", "geo_indicator"),
           description=(
               "Gross value of landings: what a fisher means by revenue. "
               "Use total_income instead to include other income, quota "
               "leasing income and operating subsidies.")),
    Metric("fleet_total_income", "Fishing-fleet total income",
           "eu-dcf-fleet-economics", "total_income", "sum", "flow", "EUR",
           ("country_name", "country_code", "supra_reg", "fishing_tech",
            "vessel_length", "geo_indicator"),
           description=(
               "Landings value plus other income, quota leasing income and "
               "operating subsidies.")),
    Metric("gva", "Gross value added (GVA)",
           "eu-dcf-fleet-economics", "gva", "sum", "flow", "EUR",
           ("country_name", "country_code", "supra_reg", "fishing_tech",
            "vessel_length", "geo_indicator"),
           description="Total income less energy, repair, other operating and quota costs."),
    Metric("gross_profit", "Gross profit",
           "eu-dcf-fleet-economics", "gross_profit", "sum", "flow", "EUR",
           ("country_name", "country_code", "supra_reg", "fishing_tech",
            "vessel_length", "geo_indicator"),
           description=(
               "GVA less personnel costs and the value of unpaid labour, per "
               "STECF's 2024 Annual Economic Report. Includes operating "
               "subsidies in income.")),
    Metric("net_profit", "Net profit",
           "eu-dcf-fleet-economics", "net_profit", "sum", "flow", "EUR",
           ("country_name", "country_code", "supra_reg", "fishing_tech",
            "vessel_length", "geo_indicator"),
           description=(
               "Gross profit less consumption of fixed capital. Before the "
               "opportunity cost of capital, which the AER extract omits.")),
    Metric("oecd_support_total", "Total OECD fisheries support", "oecd-fisheries-support-eu",
           "direct_transfers_meur + cost_reducing_transfers_meur + general_services_meur",
           "sum", "flow", "MEUR", ("country_code",),
           description="Sum of the three OECD support categories."),
    Metric("oecd_direct_transfers", "OECD direct transfers", "oecd-fisheries-support-eu",
           "direct_transfers_meur", "sum", "flow", "MEUR", ("country_code",)),
    Metric("oecd_cost_reducing", "OECD cost-reducing transfers", "oecd-fisheries-support-eu",
           "cost_reducing_transfers_meur", "sum", "flow", "MEUR", ("country_code",)),
    Metric("biomass", "Empirical biomass", "surimi-biomass-wmed-empirical",
           "biomass_kt", "sum", "stock", "kt", ("species_code",),
           description="Survey biomass, one stock value per year. Slice by year for the series."),
    # The social table repeats the same workforce under each breakdown (gender, employment
    # status, age, nationality, education), so the metric sums only the rows split by gender.
    Metric("female_employment", "Women employed (AER socemploy headcount)", "aer-social-data",
           "variable_value", "sum", "stock", "persons",
           ("country_name", "country_code"), eav_code="socemploy",
           fixed=(("gender", ("Female", "female")),),
           description="Headcount of women employed in the fleet, from the rows each "
                       "country reports by gender."),
    Metric("sales_value", "Simulated sales value", "surimi-sales-aegean-simulated",
           "value_meur", "sum", "flow", "MEUR", ("market_code", "species_code")),
    Metric("sales_quantity", "Simulated sales quantity", "surimi-sales-aegean-simulated",
           "quantity_kt", "sum", "flow", "kt", ("market_code", "species_code")),
    Metric("fdi_vessels", "Fleet vessels (FDI)", "fdi-capacity-by-country",
           "total_vessels", "sum", "stock", "vessels",
           ("country", "fishing_tech", "vessel_length_category")),
    Metric("implied_fuel_price", "Implied fuel price", "v-fuel-price-by-country-year",
           "implied_fuel_price", "avg", "ratio", "EUR/litre",
           ("country_code", "country_name", "year"),
           description="Energy cost divided by energy consumption; the diesel "
                       "price a fleet effectively paid, not a posted price."),
    Metric("energy_cost_share", "Energy cost share of landings income", "v-fuel-price-by-country-year",
           "energy_cost_share", "avg", "ratio", "ratio",
           ("country_code", "country_name", "year"),
           description="Energy cost divided by landings income; a share of "
                       "revenue, 0-1 in a normal year."),
)
REGISTRY: tuple[Metric, ...] = tuple(
    replace(m, origin=catalog.dataset_kind(m.dataset)) for m in _REGISTRY_PRE
)


def resolve(name: str, registry: Sequence[Metric] = REGISTRY) -> Metric:
    """Return the metric called `name`, or raise MetricError listing the valid names."""
    for m in registry:
        if m.name == name:
            return m
    raise MetricError(f"unknown metric {name!r}; choices: {[m.name for m in registry]}")


# Country code to name. The FDI table stores "Spain" and the AER tables store "ESP", and a question can
# use either. This table only translates between them. The form a column holds is read from the column.
_ISO3_TO_NAME = {
    "BEL": "Belgium", "BGR": "Bulgaria", "HRV": "Croatia", "CYP": "Cyprus",
    "DNK": "Denmark", "EST": "Estonia", "FIN": "Finland", "FRA": "France",
    "DEU": "Germany", "GRC": "Greece", "IRL": "Ireland", "ITA": "Italy",
    "LVA": "Latvia", "LTU": "Lithuania", "MLT": "Malta", "NLD": "Netherlands",
    "POL": "Poland", "PRT": "Portugal", "ROU": "Romania", "SVN": "Slovenia",
    "ESP": "Spain", "SWE": "Sweden",
}
_NAME_TO_ISO3 = {v.lower(): k for k, v in _ISO3_TO_NAME.items()}


def _column_values(cube: Cube, dataset: str, col: str) -> set[str]:
    """Return the distinct non-null values of a column as strings, or an empty set if the query fails."""
    try:
        rows = cube.query(
            f"SELECT DISTINCT {_q(col)} AS v FROM {_q(dataset)} "
            f"WHERE {_q(col)} IS NOT NULL")
    except Exception:
        return set()
    return {str(r["v"]) for r in rows}


def _translate_filter_values(
    cube: Cube, dataset: str, filters: Sequence[dict],
) -> tuple[list[dict], list[str]]:
    """Rewrite `=` and `in` filter values with string values into the form the column stores.

    It matches case-insensitively and swaps a country code for its name, or the reverse, when only that form is in the column.
    It returns the rewritten filters and the values that match under neither form.
    """
    out: list[dict] = []
    unmatched: list[str] = []
    for f in filters:
        col, op = f.get("col"), f.get("op")
        raw = f.get("value")
        if not col or op not in ("=", "in") or raw is None:
            out.append(f)
            continue
        wanted = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        if not all(isinstance(w, str) for w in wanted):
            out.append(f)
            continue

        present = _column_values(cube, dataset, col)
        if not present:
            out.append(f)
            continue
        lowered = {v.lower(): v for v in present}

        resolved: list[str] = []
        for w in wanted:
            if w.lower() in lowered:
                resolved.append(lowered[w.lower()])
                continue
            # Try the other form of the same country.
            alt = _ISO3_TO_NAME.get(w.upper()) or _NAME_TO_ISO3.get(w.lower())
            if alt and alt.lower() in lowered:
                resolved.append(lowered[alt.lower()])
                continue
            unmatched.append(w)
        if not resolved:
            # Nothing matched, so the original filter stays and the query returns no rows.
            # Dropping the filter would answer a wider question.
            out.append(f)
            continue
        out.append({**f, "value": resolved if op == "in" else resolved[0],
                    "op": "in" if len(resolved) > 1 else op})
    return out, unmatched


def run_query_metric(
    cube: Cube,
    name: str,
    by: Optional[Sequence[str]] = None,
    where: Optional[Sequence[dict]] = None,
    year: Optional[Any] = None,
    order: Optional[Sequence[dict]] = None,
    limit: Optional[int] = None,
    registry: Sequence[Metric] = REGISTRY,
) -> dict:
    """Calculate the named metric on the cube, sliced by `by`, filtered by `where` and pinned to `year`.

    It raises MetricError for an unknown metric, a dimension the metric does not allow or an empty origin.
    It returns the envelope of `aggregate`, with the metric's name, unit and origin added to its `value`.
    """
    m = resolve(name, registry)
    if not m.origin:
        raise MetricError(f"{name}: no origin declared (observed/modelled)")
    cube_ds = _cube_dataset(m, cube)
    dims = list(by or [])
    bad = [d for d in dims if d not in m.dimensions]
    if bad:
        raise MetricError(f"{name}: cannot slice by {bad}; allowed: {list(m.dimensions)}")

    filters: list[dict] = list(where or [])
    if m.eav_code is not None:
        filters.append({"col": _DEFAULT_EAV_CODE_COL, "op": "=", "value": m.eav_code})
    for col, values in m.fixed:
        filters.append({"col": col, "op": "in", "value": list(values)})
    if year is not None:
        filters.append({"col": m.time, "op": "=", "value": year})

    filters, _unmatched_values = _translate_filter_values(cube, cube_ds, filters)

    measures = [{"col": m.measure, "fn": m.agg}]
    stock_cols = {m.measure} if m.additivity == "stock" else set()
    filters, year_defaulted, note = apply_latest_year_default(
        cube, cube_ds, m.time, measures, filters, dims, stock_cols)

    out = _aggregate(cube, cube_ds, measures=measures, dimensions=dims or None,
                     filters=filters or None, order=order, limit=limit,
                     defaulted_year_col=m.time if year_defaulted else None)
    v = out["value"]
    v["metric"] = m.name
    v["unit"] = m.unit
    # The result states whether the number is observed or modelled.
    v["data_kind"] = catalog.dataset_kind(m.dataset)
    v["origin"] = m.origin
    # The dataset's `not_a_substitute_for` text goes into the result, as it does for `aggregate`.
    _sub = catalog.substitute_warning(m.dataset)
    if _sub:
        v["not_a_substitute_for"] = _sub
    _mnote = catalog.modelled_note(m.dataset)
    if _mnote:
        _prev = v.get("notes") or ""
        if _prev.strip().lower() == "ok": _prev = ""
        v["notes"] = (_prev + " " + _mnote).strip()
    if _unmatched_values:
        v["unmatched_filter_values"] = _unmatched_values
    # A flow metric with no year pinned sums every year in the table, so the result names
    # the years covered and how to narrow them.
    if m.additivity != "stock" and not year_defaulted:
        _pinned = any(f.get("col") == m.time and f.get("op") in ("=", "in")
                      for f in filters)
        if not _pinned and m.time not in dims:
            _lo, _hi = _time_span(cube, cube_ds, m.time)
            if _lo is not None and _hi is not None and _lo != _hi:
                v["years_covered"] = f"{_lo}-{_hi}"
                _prev = v.get("notes") or ""
                if _prev.strip().lower() == "ok":
                    _prev = ""
                v["notes"] = (
                    _prev + f" Summed over {_lo}-{_hi}, every year in the table. "
                    f"State that period with the number, or pass year= "
                    f"(or group by {m.time}) for a single year.").strip()
    if year_defaulted:
        v["year_defaulted"] = True
        existing = v.get("notes") or ""
        if existing.strip().lower() == "ok": existing = ""
        v["notes"] = (existing + " " + note).strip()
    return out


def catalog_prompt(registry: Sequence[Metric] = REGISTRY) -> str:
    """Return the text that lists every metric with its time rule, unit and dimensions.

    The `list_metrics` tool returns it as `prompt`.
    """
    lines = [
        "METRICS -- call query_metric(metric, by=[dims], where=[{col,op,value}], year=...) "
        "to get any number. The metric owns the table, aggregation, unit and time rule, "
        "so you cannot pick the wrong table or sum a per-year stock across years. "
        "Slice with `by`; filter with `where`; pin a year with `year`.",
        "",
    ]
    for m in registry:
        time_rule = ("stock: defaults to the latest year unless you pass year/ group by year"
                     if m.additivity == "stock" else
                     "flow: sums across years" if m.additivity == "flow" else
                     "ratio: averaged, never summed")
        lines.append(
            f"- {m.name} -- {m.label} [{time_rule}; unit {m.unit}]; "
            f"by: {', '.join(m.dimensions)}"
            + (f"; binds variable_code={m.eav_code}" if m.eav_code else "")
            + "".join(f"; binds {col}={values[0]}" for col, values in m.fixed))
    return "\n".join(lines)
