"""Executors that run one analysis on the DuckDB cube and return it in a fixed envelope.

Each `run_*` function returns the `build_envelope` fields: a finding, the rows, a chart
specification, the provenance and the calculation records. All but `run_explain_provenance` take
their numbers from `aggregate()`. The LLM only repeats the finding. The server calculates the numbers
and signs each record.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from surimi_data_mcp.engine.aggregate import aggregate, apply_latest_year_default, _q

GROUNDING_RULE = """GROUNDING RULE (strict). You are a data tool, not an oracle.
- Only state facts that came from a tool response in THIS conversation.
- If a tool returned an error, null, zero, or empty result, say so explicitly.
- Never invent numbers. Never extrapolate beyond the data returned.
- Cite the tool call that produced each number.
- If the user asks about a topic not covered by any tool, say "This is outside the data I have access to."
- Separate clearly what is DATA (from tools) and what is your INTERPRETATION."""


def _period_key(v: Any):
    """Sort key: numeric periods (years) before text, each in natural order."""
    try:
        return (0, float(v))
    except (TypeError, ValueError):
        return (1, str(v))


def _coverage(rows: Sequence[dict], time_col: str) -> Optional[str]:
    """Return the first and last period in `rows` as `first-last`, or None when no row has one."""
    periods = sorted({r[time_col] for r in rows if r.get(time_col) is not None},
                     key=_period_key)
    if not periods:
        return None
    return f"{periods[0]}-{periods[-1]}"


def _engine_notes(value) -> list:
    """Turn the `notes` string of an `aggregate()` value into a list, where "ok" or an empty string gives an empty list.

    `aggregate()` returns `notes` as one string, so `list()` on it would split it into characters.
    """
    n = value.get("notes")
    if isinstance(n, str):
        n = n.strip()
        return [] if (not n or n.lower() == "ok") else [n]
    return list(n or [])


def trend_finding(rows, time_col, group_col, value_key):
    """Describe the trend of `value_key` over `time_col` for each group in `rows`.

    Returns `(text, summary)`. The text gives each group's direction, first and last value, and range.
    The summary holds those values for each group, with the period of the largest step between
    neighbouring periods as `inflection_period`.
    """
    by_group: dict[Any, list] = {}
    for r in rows:
        g = r.get(group_col) if group_col else "_all"
        by_group.setdefault(g, []).append((r.get(time_col), r.get(value_key)))
    parts, summary = [], {}
    for g, pairs in by_group.items():
        pairs = [(p, v) for p, v in pairs if v is not None]
        if not pairs:
            continue
        pairs.sort(key=lambda pv: _period_key(pv[0]))
        first_p, first_v = pairs[0]
        last_p, last_v = pairs[-1]
        vmin = min(v for _, v in pairs)
        vmax = max(v for _, v in pairs)
        direction = ("increasing" if last_v > first_v
                     else "decreasing" if last_v < first_v else "stable")
        infl_p, infl_d = None, 0.0
        for (p0, v0), (p1, v1) in zip(pairs, pairs[1:]):
            d = abs(v1 - v0)
            if d > infl_d:
                infl_d, infl_p = d, p1
        summary[g] = {"direction": direction, "first": first_v, "last": last_v,
                      "min": vmin, "max": vmax, "first_period": first_p,
                      "last_period": last_p, "inflection_period": infl_p}
        label = "" if g == "_all" else f"{g}: "
        parts.append(f"{label}{direction} {first_v:g} -> {last_v:g} "
                     f"({first_p}-{last_p}), range {vmin:g}-{vmax:g}")
    finding = "; ".join(parts) if parts else "No non-null values to analyze."
    return finding, summary


def chart_from_rows(rows, time_col, group_col, value_key, kind="line"):
    """Build a chart specification with the periods as `labels` and `value_key` as the values.

    With `group_col` the specification has one series per group, each aligned to the labels.
    Without it, it has a single `values` list.
    """
    labels = sorted({r[time_col] for r in rows if r.get(time_col) is not None},
                    key=_period_key)
    label_strs = [str(l) for l in labels]
    if group_col:
        groups = sorted({r.get(group_col) for r in rows
                         if r.get(group_col) is not None}, key=str)
        series = []
        for g in groups:
            vmap = {r[time_col]: r.get(value_key) for r in rows
                    if r.get(group_col) == g}
            series.append({"name": str(g), "values": [vmap.get(l) for l in labels]})
        return {"kind": kind, "labels": label_strs, "series": series}
    vmap = {r[time_col]: r.get(value_key) for r in rows}
    return {"kind": kind, "labels": label_strs, "values": [vmap.get(l) for l in labels]}


def build_envelope(skill, dataset, params_used, finding, data, chart,
                   provenance, notes, receipts):
    """Return the envelope that every executor produces, with one key for each argument."""
    return {"skill": skill, "dataset": dataset, "params_used": params_used,
            "finding": finding, "data": data, "chart": chart,
            "provenance": provenance, "notes": notes, "receipts": receipts}


def run_analyze_trend(cube, *, dataset_name, time_column, metric,
                      group_by=None, period=None, agg="sum", code_labels=None):
    """Aggregate `metric` by `time_column`, and by `group_by` when given, and return a trend envelope.

    `period` keeps the rows from that value onward, and `agg` is the aggregate function name.
    """
    dims = [time_column] + ([group_by] if group_by else [])
    filters = [{"col": time_column, "op": ">=", "value": period}] if period else []
    out = aggregate(cube, dataset_name,
                    measures=[{"col": metric, "fn": agg}],
                    dimensions=dims, filters=filters,
                    order=[{"col": time_column, "dir": "asc"}])
    value = out["value"]
    rows = value["rows"]
    value_key = f"{metric}_{agg}"
    finding, summary = trend_finding(rows, time_column, group_by, value_key)
    chart = chart_from_rows(rows, time_column, group_by, value_key, kind="line")
    notes = _engine_notes(value)
    if group_by and not (code_labels or {}):
        notes.append("group codes shown verbatim; no common-name mapping in the dataset")
    provenance = {"dataset": dataset_name, "metric": metric, "group_by": group_by,
                  "time_column": time_column, "coverage": _coverage(rows, time_column)}
    return build_envelope("analyze_trend", dataset_name,
                          {"metric": metric, "group_by": group_by,
                           "agg": agg, "period": period},
                          finding, {"rows": rows, "summary": summary},
                          chart, provenance, notes, [out["receipt"]])


# Columns that may hold a country. `_resolve_country_column` tries the default column first and
# then these in order.
_COUNTRY_COL_CANDIDATES = ("country_code", "country_name", "country", "flag_country")


def _resolve_country_column(cube, dataset_name, requested, default_col):
    """Pick the column in which the requested country values appear, by reading the data.

    The function queries the distinct values of `default_col` and then of each column in
    `_COUNTRY_COL_CANDIDATES` that the dataset has, and keeps the column that matches the most
    requested values, ignoring case. It returns `(column, unmatched_values)`. When nothing is requested
    or nothing matches, it returns `default_col`.
    """
    wanted = [str(c) for c in (requested or [])]
    if not wanted:
        return default_col, []
    try:
        present = {c["name"] for c in cube.columns(dataset_name)}
    except Exception:
        return default_col, []

    ordered = [default_col] + [c for c in _COUNTRY_COL_CANDIDATES if c != default_col]
    best_col, best_matched = default_col, set()
    lowered = {w.lower() for w in wanted}
    for col in ordered:
        if col not in present:
            continue
        try:
            rows = cube.query(
                f"SELECT DISTINCT {_q(col)} AS v FROM {_q(dataset_name)} "
                f"WHERE {_q(col)} IS NOT NULL")
        except Exception:
            continue
        values = {str(r["v"]) for r in rows}
        matched = {v for v in values if v.lower() in lowered}
        if len(matched) > len(best_matched):
            best_col, best_matched = col, matched
        if len(best_matched) == len(lowered):
            break

    matched_lower = {m.lower() for m in best_matched}
    unmatched = [w for w in wanted if w.lower() not in matched_lower]
    return best_col, unmatched


def run_compare_countries(cube, *, dataset_name, time_column, metric,
                          countries=None, period=None, country_col="country_code",
                          agg="sum"):
    """Rank `countries` by `metric`, highest first, and return a comparison envelope.

    `_resolve_country_column` picks the column in which most of the requested values appear, ignoring
    case. `period` filters to that one value of `time_column`. A requested value that matches no column
    is listed in `data["unmatched"]`, and in the finding or the notes.
    """
    country_col, unmatched = _resolve_country_column(
        cube, dataset_name, countries, country_col)
    filters = []
    if countries:
        filters.append({"col": country_col, "op": "in", "value": list(countries)})
    if period:
        filters.append({"col": time_column, "op": "=", "value": period})
    out = aggregate(cube, dataset_name,
                    measures=[{"col": metric, "fn": agg}],
                    dimensions=[country_col], filters=filters,
                    order=[{"col": f"{metric}_{agg}", "dir": "desc"}])
    value = out["value"]
    rows = value["rows"]
    value_key = f"{metric}_{agg}"
    ranking = [{"country": r.get(country_col), "value": r.get(value_key)}
               for r in rows if r.get(value_key) is not None]
    vals = [r["value"] for r in ranking]
    gap_ratio = (max(vals) / min(vals)) if vals and min(vals) else None
    chart = {"kind": "bar",
             "labels": [str(r["country"]) for r in ranking],
             "values": [r["value"] for r in ranking]}
    if ranking:
        finding = (f"{ranking[0]['country']} highest ({ranking[0]['value']:g}), "
                   f"{ranking[-1]['country']} lowest ({ranking[-1]['value']:g})"
                   + (f"; {gap_ratio:.2g}x gap" if gap_ratio else ""))
    elif unmatched:
        finding = (f"Not recognised in {dataset_name}: {', '.join(unmatched)}. "
                   f"No comparison was run; this is an unmatched input, not a "
                   f"gap in the data.")
    else:
        finding = "No non-null values to compare."
    notes = list(_engine_notes(value))
    if unmatched and ranking:
        notes.append(
            f"Not matched and therefore not compared: {', '.join(unmatched)}.")
    provenance = {"dataset": dataset_name, "metric": metric,
                  "country_column": country_col, "coverage": _coverage(rows, time_column)}
    return build_envelope("compare_countries", dataset_name,
                          {"metric": metric, "countries": countries,
                           "period": period, "agg": agg},
                          finding, {"ranking": ranking, "gap_ratio": gap_ratio,
                                    "unmatched": unmatched},
                          chart, provenance, notes,
                          [out["receipt"]])


def _lineage_sentence(entry: dict) -> str:
    """Write the sentences that name a source file, its publisher, row count, years, checksum and URL, where the manifest entry has them."""
    bits = [f"These rows were built from {entry['file']}"]
    if entry.get("agency") and entry["agency"] != "unknown":
        bits.append(f"published by {entry['agency']}")
    out = " ".join(bits) + "."
    n_rows = entry.get("n_rows")
    if n_rows is not None:
        out += f" It holds {n_rows:,} rows."
    first, last = entry.get("first"), entry.get("last")
    if first is not None and last is not None:
        out += f" The years present in it run {first}-{last}."
        missing = entry.get("missing") or []
        if missing:
            out += f" These years are absent from that range: {missing}."
    if entry.get("sha256"):
        out += f" The file checksummed sha256 {entry['sha256']} at build time."
    if entry.get("original_url"):
        out += f" It was downloaded from {entry['original_url']}."
    return out


def run_explain_provenance(index, *, dataset_id, question=None, top_k=5, secret=None):
    """Search the SURIMI documents for `question` and return the passages with the dataset's source file entry.

    The envelope holds up to `top_k` passages and the build manifest entry from
    `catalog.source_for_dataset`. It carries a signed record only when `secret` is set and there is
    something to sign.
    """
    query = question or dataset_id
    results = index.search(query, top_k=top_k)
    passages = [{"text": r["text"], "source": r["source"]} for r in results]
    sources = sorted({p["source"] for p in passages})

    # The build manifest records the source file, its sha256, the number of rows read and the year
    # span. It goes into the result so that the LLM reads it.
    from surimi_data_mcp.datasets import catalog
    lineage_entry = catalog.source_for_dataset(dataset_id)

    receipts = []
    if secret and (passages or lineage_entry):
        from surimi_data_mcp.provenance.receipts import issue_receipt
        receipts = [issue_receipt(
            tool_id="explain_provenance",
            input_params={"dataset_id": dataset_id, "question": query},
            output_value={"passages": passages, "lineage": lineage_entry},
            secret=secret)]

    findings, notes = [], []
    if lineage_entry:
        findings.append(_lineage_sentence(lineage_entry))
    if passages:
        findings.append(
            f"Found {len(passages)} provenance passage(s) in: " + ", ".join(sources))
    if not findings:
        findings.append("No SURIMI documentation matched this dataset/question.")
        notes.append("the SURIMI documents do not cover this; do not fill the gap with general knowledge")
    elif not passages:
        # The manifest names the file, and the documents do not describe how the data was collected.
        notes.append("this is file-level lineage from the build manifest; the SURIMI documents "
                     "do not describe the collection methodology for this dataset")

    return build_envelope("explain_provenance", dataset_id,
                          {"question": query, "top_k": top_k},
                          " ".join(findings),
                          {"passages": passages, "lineage": lineage_entry,
                           "documented_use": catalog.DOCUMENTED_USE}, None,
                          {"sources": sources}, notes, receipts)


def run_generate_report(cube, index, *, dataset_name, time_column, columns,
                        focus, period=None, dataset_id=None, secret=None):
    """Build a report envelope with a row count, the trend of the first numeric column, and the provenance passages.

    Each number comes from an `aggregate()` call, and the provenance section comes from
    `run_explain_provenance`.
    """
    numeric = [c["name"] for c in columns
               if c["type"] in ("float", "int", "double") and c["name"] != time_column]
    receipts, sections = [], {}
    rng = [{"col": time_column, "op": ">=", "value": period}] if period else []

    cnt = aggregate(cube, dataset_name,
                    measures=[{"col": time_column, "fn": "count"}], dimensions=[],
                    filters=rng)
    receipts.append(cnt["receipt"])
    cnt_rows = cnt["value"]["rows"]
    sections["overview"] = {
        "row_count": cnt_rows[0].get(f"{time_column}_count") if cnt_rows else 0}

    key = numeric[0] if numeric else None
    if key:
        rk = aggregate(cube, dataset_name, measures=[{"col": key, "fn": "sum"}],
                       dimensions=[time_column], filters=rng,
                       order=[{"col": time_column, "dir": "asc"}])
        receipts.append(rk["receipt"])
        vk = f"{key}_sum"
        trend, _ = trend_finding(rk["value"]["rows"], time_column, None, vk)
        sections["key_metric"] = {
            "metric": key, "trend": trend,
            "chart": chart_from_rows(rk["value"]["rows"], time_column, None, vk)}

    prov = run_explain_provenance(index, dataset_id=dataset_id or dataset_name,
                                  question=focus, secret=secret)
    receipts.extend(prov["receipts"])
    sections["provenance"] = prov["data"]["passages"]

    finding = (f"Report on {dataset_name} (focus: {focus}); "
               f"{len(numeric)} numeric metric(s); "
               f"{sections['overview']['row_count']} rows.")
    notes = ["the row count and the trend are calculated by the engine, each with a calculation record in receipts"]
    return build_envelope("generate_report", dataset_name,
                          {"focus": focus, "period": period},
                          finding, {"sections": sections}, None,
                          {"numeric_columns": numeric}, notes, receipts)


def run_breakdown(cube, *, dataset_name, group_by, metric=None, fn=None,
                  variable_code=None, eav_code_col="variable_code", columns=None,
                  stock_cols=None, time_column="year", year=None):
    """Rank the categories of `group_by` by `metric`, or by row count when `metric` is None.

    On an attribute-coded table, a sum, average, minimum or maximum without `variable_code` returns
    the available codes under `data["needs"]` and runs no query on the metric. For a measure listed
    in `stock_cols`, such as a vessel count, with no `year` given, the function filters to the latest
    value of `time_column`, because summing a stock across years counts it once per year. A given
    `year` always filters the rows to that year.
    """
    names = {c["name"] for c in (columns or [])}
    is_eav = eav_code_col in names
    if metric is None:
        measures = [{"col": group_by, "fn": "count"}]
        value_key = f"{group_by}_count"
        filters = []
    else:
        agg = fn or "sum"
        if is_eav and agg in ("sum", "avg", "min", "max") and not variable_code:
            codes_out = aggregate(cube, dataset_name,
                                  measures=[{"col": eav_code_col, "fn": "count"}],
                                  dimensions=[eav_code_col])
            codes = sorted(r.get(eav_code_col) for r in codes_out["value"]["rows"])
            return build_envelope(
                "breakdown", dataset_name,
                {"group_by": group_by, "metric": metric, "fn": agg},
                (f"{dataset_name} is attribute-coded; pick one {eav_code_col} to "
                 f"break {metric} down by {group_by}."),
                {"needs": {eav_code_col: codes}, "ranking": []}, None,
                {"dataset": dataset_name, "group_by": group_by},
                [f"choose one {eav_code_col} of {codes}"], [])
        measures = [{"col": metric, "fn": agg}]
        value_key = f"{metric}_{agg}"
        filters = ([{"col": eav_code_col, "op": "=", "value": variable_code}]
                   if (is_eav and variable_code) else [])

    year_note = None
    _defaulted = False
    if year is not None:
        filters = list(filters) + [{"col": time_column, "op": "=", "value": str(year)}]
    else:
        filters, _defaulted, year_note = apply_latest_year_default(
            cube, dataset_name, time_column, measures, filters, [group_by],
            set(stock_cols or ()))

    out = aggregate(cube, dataset_name, measures=measures, dimensions=[group_by],
                    filters=filters, order=[{"col": value_key, "dir": "desc"}],
                    defaulted_year_col=time_column if _defaulted else None)
    value = out["value"]
    ranking = [{"category": r.get(group_by), "value": r.get(value_key)}
               for r in value["rows"]]
    chart = {"kind": "bar",
             "labels": [str(r["category"]) for r in ranking],
             "values": [r["value"] for r in ranking]}
    if ranking:
        finding = (f"{ranking[0]['category']} highest ({ranking[0]['value']:g}), "
                   f"{ranking[-1]['category']} lowest ({ranking[-1]['value']:g}) "
                   f"across {len(ranking)} {group_by} categories")
    else:
        finding = "No non-null categories to break down."
    provenance = {"dataset": dataset_name, "group_by": group_by,
                  "metric": metric or f"count({group_by})"}
    return build_envelope("breakdown", dataset_name,
                          {"group_by": group_by, "metric": metric, "fn": fn,
                           "variable_code": variable_code, "year": year},
                          finding, {"ranking": ranking}, chart, provenance,
                          _engine_notes(value) + ([year_note] if year_note else []),
                          [out["receipt"]])
