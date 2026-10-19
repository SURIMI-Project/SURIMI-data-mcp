"""Writes the explanation that a receipt carries for the reader of an answer.

The server writes the sentence from the query it ran, the engine's own disclosures and the dataset
catalogue, not from the LLM. It is stored in the `human_check` block of the receipt, next to the SQL,
inside the signed body.

The chat shows the sentence as markdown. Every column, filter value and table name that the LLM
chose goes into a code span with line breaks removed and a backtick fence longer than any run inside
it, so it cannot become a link, an image or a new line of the answer. Catalogue names come from this
repository and are shown as text.

The receipt keeps only the sentence and the ids of the datasets read. `dataset_notes()` returns the
catalogue's notes on one dataset for the audit page.
"""
from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from typing import Any

from surimi_data_mcp.datasets import catalog

_FN_WORDS = {
    "sum": "sum of {c}",
    "avg": "average of {c}",
    "count": "number of non-empty values of {c}",
    "count_distinct": "number of distinct values of {c}",
    "min": "lowest {c}",
    "max": "highest {c}",
}
_OP_WORDS = {
    "=": "is", "!=": "is not", "<>": "is not", "<": "is below", "<=": "is at most",
    ">": "is above", ">=": "is at least", "in": "is one of", "not in": "is not one of",
}
_SQL_SUMMARY = "A read-only SQL query written by the assistant for this question"
_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]+")


def _clean(text: Any) -> str:
    """One line of text: control characters and line breaks become single spaces."""
    return _CONTROL.sub(" ", str(text)).strip()


def _code(text: Any) -> str:
    """A CommonMark code span no content can close early: the fence is one backtick longer than the
    longest run inside, padded with a space when the content starts or ends with a backtick."""
    s = _clean(text)
    longest = max((len(r) for r in re.findall(r"`+", s)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if s.startswith("`") or s.endswith("`") or not s else ""
    return f"{fence}{pad}{s}{pad}{fence}"


def _value(v: Any) -> str:
    """A filter value in words: `empty`, `true`, `false`, a bare number, or a quoted code span."""
    if v is None:
        return "empty"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return _code("'" + str(v) + "'")


def _text(s: Any) -> str:
    """Catalogue text shown outside a code span: one line, and no markdown link or image syntax."""
    return _clean(s).replace("](", "] (").replace("![", "! [").replace("<", "‹")


def _join(parts: Sequence[str]) -> str:
    """Join parts as `a, b and c`."""
    parts = list(parts)
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _lookup(name: Any) -> dict[str, Any] | None:
    """Return the catalogue entry for a dataset id or table name, matched exactly, or None.

    `catalog.find_dataset` also matches partial names, so a near miss would name the wrong table here.
    """
    needle = catalog._norm(str(name))
    for d in catalog.DATASETS:
        table = d.get("table", "")
        parts = table.split(".")
        names = {d["id"], table, parts[-1], ".".join(parts[-2:])}
        if needle in {catalog._norm(n) for n in names if n}:
            return d
    return None


def _columns(d: dict[str, Any]) -> list[dict[str, Any]]:
    """The column entries of a catalogue dataset, parsing them when they are stored as a string."""
    cols = d.get("columns") or []
    if isinstance(cols, str):
        try:
            cols = ast.literal_eval(cols)
        except (ValueError, SyntaxError):
            return []
    return [c for c in cols if isinstance(c, dict)]


def dataset_notes(dataset_id: str) -> dict[str, Any] | None:
    """Return the catalogue's notes on one dataset for the audit page's "About this table" card, or None.

    The result has `id`, `name` and `kind`, and `note`, `citation` and `columns` when the catalogue has them.
    """
    d = _lookup(dataset_id)
    if d is None:
        return None
    out: dict[str, Any] = {"id": d["id"], "name": d.get("name", d["id"]), "kind": d.get("kind", "")}
    note = " ".join(str(d[k]).strip() for k in ("warning", "scope_note") if d.get(k))
    if note:
        out["note"] = note
    if d.get("citation"):
        out["citation"] = str(d["citation"]).strip()
    described = [{"name": c["name"], "description": c["description"]}
                 for c in _columns(d) if c.get("name") and c.get("description")]
    if described:
        out["columns"] = described
    return out


def _short_name(d: dict[str, Any]) -> str:
    """The catalogue name without its parenthetical note, which the audit page shows."""
    name = str(d.get("name", d["id"]))
    return name.split(" (", 1)[0] if " (" in name else name


def _table_words(name: Any, d: dict[str, Any] | None) -> str:
    """The catalogue name in quotes when the dataset is known, otherwise `table` and the name as code."""
    return f"“{_text(_short_name(d))}”" if d else f"table {_code(name)}"


def _filter_words(f: dict[str, Any], defaulted_year_col: str | None) -> str:
    """One filter in words, noting an `=` filter on the year column that the caller set by default."""
    op = str(f.get("op", "=")).lower()
    val = f.get("value", f.get("values"))
    col = _code(f.get("col"))
    if op in ("in", "not in") and isinstance(val, (list, tuple)):
        return f"{col} {_OP_WORDS[op]} " + ", ".join(_value(v) for v in val)
    if val is None and op in ("=", "!=", "<>"):
        return f"{col} {'is empty' if op == '=' else 'is not empty'}"
    words = f"{col} {_OP_WORDS.get(op, _code(op))} {_value(val)}"
    if defaulted_year_col and f.get("col") == defaulted_year_col and op == "=":
        words += " (the latest year, chosen by default)"
    return words


def _years(span: Any) -> str | None:
    """Turn a `first-last` span into `first to last`, or None when it does not have that shape."""
    m = re.fullmatch(r"\s*(\S+?)\s*-\s*(\S+)\s*", str(span or ""))
    return f"{_clean(m.group(1))} to {_clean(m.group(2))}" if m else None


def _disclosures(value: dict[str, Any]) -> list[str]:
    """The sentences for what the engine reported about this result: years summed, rows left out, merged spellings."""
    out: list[str] = []
    years = _years(value.get("years_covered"))
    if years:
        out.append(f"Summed over every year in the table, {years}.")
    suppressed = [int(n) for n in (value.get("suppressed") or {}).values() if isinstance(n, int)]
    if suppressed and max(suppressed) > 0:
        n = max(suppressed)
        out.append(f"This is a lower bound: {n} matching row{'s' if n != 1 else ''} reported no value "
                   f"and {'are' if n != 1 else 'is'} left out.")
    nulls = {d: int(n) for d, n in (value.get("null_dimensions_excluded") or {}).items()
             if isinstance(n, int) and n > 0}
    if nulls:
        n = sum(nulls.values())
        out.append(f"{n} row{'s' if n != 1 else ''} with no {_join([_code(d) for d in nulls])} value "
                   f"{'are' if n != 1 else 'is'} left out.")
    merged = value.get("normalized_dimensions")
    if merged:
        cols = list(merged) if isinstance(merged, dict) else [m.get("dimension", m) if isinstance(m, dict) else m
                                                               for m in merged]
        out.append(f"Spelling variants of {_join([_code(c) for c in cols])} were merged.")
    return out


def explain_aggregate(dataset: str, measures: Sequence[dict], dimensions: Sequence[str],
                      filters: Sequence[dict], *, having: Sequence[dict] = (), order: Sequence[dict] = (),
                      limit: int | None = None, value: dict[str, Any] | None = None,
                      defaulted_year_col: str | None = None) -> dict[str, Any]:
    """Return `{"summary", "datasets"}` for an `aggregate` call.

    The summary is one sentence on what was calculated, followed by the engine's disclosures about the result.
    """
    value = value or {}
    d = _lookup(dataset)
    what = _join([_FN_WORDS.get(str(m.get("fn", "")).lower(), "{c}").format(c=_code(m.get("col")))
                  for m in measures]) or "rows"
    sentence = f"{what} in {_table_words(dataset, d)}"
    if dimensions:
        sentence += ", by " + _join([_code(x) for x in dimensions])
    if filters:
        sentence += ", where " + _join([_filter_words(f, defaulted_year_col) for f in filters])
    if having:
        sentence += ", keeping only groups where " + _join([
            f"{_code(h.get('agg'))} {_OP_WORDS.get(str(h.get('op', '=')).lower(), _code(h.get('op')))} "
            f"{_value(h.get('value'))}" for h in having])
    if limit:
        of = f" of {int(value['total_groups'])}" if value.get("truncated") and value.get("total_groups") else ""
        if order:
            first = order[0]
            side = "bottom" if str(first.get("dir", "asc")).lower() == "asc" else "top"
            sentence += f", the {side} {int(limit)}{of} by {_code(first.get('col'))}"
        else:
            sentence += f", the first {int(limit)}{of} groups"
    sentence = sentence[0].upper() + sentence[1:] + "."
    summary = " ".join([sentence] + _disclosures(value))
    return {"summary": summary, "datasets": [d["id"]] if d else []}


def _sql_tables(sql: str) -> list[str]:
    """The table names a query reads, excluding its CTEs, parsed as Trino, PostgreSQL or DuckDB SQL."""
    import sqlglot
    from sqlglot import exp

    for dialect in ("trino", "postgres", "duckdb"):
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except (sqlglot.errors.SqlglotError, ValueError, RecursionError):
            continue
        if tree is None:
            continue
        ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
        out: list[str] = []
        for t in tree.find_all(exp.Table):
            name = ".".join(p for p in (t.catalog, t.db, t.name) if p)
            if name and name not in out and not (not t.db and t.name in ctes):
                out.append(name)
        return out
    return []


def explain_sql(sql: str) -> dict[str, Any]:
    """Return `{"summary", "datasets"}` for a query the LLM wrote itself, naming the catalogue tables it reads."""
    tables = _sql_tables(sql)
    if not tables:
        return {"summary": _SQL_SUMMARY + ".", "datasets": []}
    found = [(t, _lookup(t)) for t in tables]
    return {"summary": _SQL_SUMMARY + ", over " + _join([_table_words(t, d) for t, d in found]) + ".",
            "datasets": [d["id"] for _, d in found if d]}
