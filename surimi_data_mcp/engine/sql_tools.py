"""Checks on SQL text and the signed `query_data` function.

The refusal functions (`read_only_refusal`, `eav_magnitude_refusal` and
`landings_double_count_refusal`) return a message when a query must not run, and `None` otherwise.
`CubeFallbackClient` reruns a query on the DuckDB cube when the primary backend lacks a table.
`make_query_data_signed(client_factory)` builds a function that runs the SQL on the client and signs
the result with an HMAC calculation record. The factory lets production code pass `default_client`
and lets tests pass a fake client.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from surimi_data_mcp.provenance import explain, lineage
from surimi_data_mcp.provenance.signed_tool import with_receipt

_AGG_CALL = re.compile(
    r"\b(sum|avg|count|min|max|count_distinct|stddev|median)\s*\(",
    re.IGNORECASE,
)
_GROUP_BY = re.compile(r"\bgroup\s+by\b", re.IGNORECASE)

# Error messages that mean the table does not exist. `CubeFallbackClient` uses them to decide when
# to rerun a query on the DuckDB cube.
_TABLE_MISS = re.compile(
    r"does not exist|not found|no such table|undefined table|relation .*does not exist",
    re.IGNORECASE,
)


def is_aggregate_sql(sql: str) -> bool:
    """Return True when `sql` contains an aggregate function call or a `GROUP BY` clause.

    A name such as `summary` or `minimum_col` does not match, because the pattern needs a word boundary and an open parenthesis.
    """
    return bool(_AGG_CALL.search(sql) or _GROUP_BY.search(sql))


# `variable_value` is the magnitude column of the AER social and economic tables, which store one
# row per attribute. Its rows come in overlapping strata (by gender, age, geo_indicator,
# fishing_tech and others) under one `variable_code`, so a raw SUM or AVG counts the same people more
# than once. COUNT tallies rows and is not affected.
#
# The check does not parse the SQL. A query is refused when a magnitude aggregate call and the token
# `variable_value` both appear anywhere in it, which also catches `SUM(CAST(variable_value AS DOUBLE))`,
# a `CASE` wrapper and `TRY_CAST`. Filters use `variable_code`, which does not match the token.
_MAGNITUDE_AGG_CALL = re.compile(
    r"\b(sum|avg|min|max|stddev|median)\s*\(",
    re.IGNORECASE,
)
_VARIABLE_VALUE_TOKEN = re.compile(r"\bvariable_value\b", re.IGNORECASE)

READ_ONLY_REFUSAL_PREFIX = "query_data is read-only"


def read_only_refusal(sql: str) -> str | None:
    """Return a refusal message unless `sql` is exactly one read statement, and `None` when it is.

    The accepted forms are one `SELECT` (including `WITH ... SELECT`, `UNION`, `EXCEPT` and `INTERSECT`)
    with no write statement anywhere in its parse tree, and `SHOW`. `sqlglot` parses the text with the
    `trino`, `postgres` and `duckdb` dialects in turn, and SQL that none of them parses is refused.
    `PostgresClient` also opens a read-only session, which is the second check.
    """
    import sqlglot
    from sqlglot import exp

    refusal = (f"{READ_ONLY_REFUSAL_PREFIX}: send one SELECT statement (WITH ... SELECT and UNION "
               "are fine). Writes, several statements, transaction and session commands are refused, "
               "and so is SQL that cannot be parsed: if this was a read, write every alias with AS "
               "and try again.")
    statements = None
    for dialect in ("trino", "postgres", "duckdb"):
        try:
            statements = sqlglot.parse(sql or "", read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
            break
        except sqlglot.errors.ParseError:
            continue
    if statements is None:
        return refusal
    statements = [st for st in statements if st is not None]
    if len(statements) != 1:
        return refusal
    st = statements[0]
    if isinstance(st, exp.Command) and str(st.this).upper() == "SHOW":
        return None
    if not isinstance(st, (exp.Select, exp.SetOperation)):
        return refusal
    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Drop, exp.Create, exp.Alter,
              exp.Command, exp.TruncateTable, exp.Set, exp.Commit, exp.Rollback, exp.Transaction)
    if any(isinstance(node, writes) for node in st.walk()):
        return refusal
    return None


def eav_magnitude_refusal(sql: str) -> str | None:
    """Return a refusal message when `sql` calls a magnitude aggregate and names `variable_value`, and `None` otherwise.

    The aggregates are `SUM`, `AVG`, `MIN`, `MAX`, `STDDEV` and `MEDIAN`. The refusal sends the caller to the `aggregate` or `breakdown` tool grouped by one dimension.
    A wrapper such as `CAST`, `CASE` or `TRY_CAST` around the column does not avoid it.
    """
    if not (_MAGNITUDE_AGG_CALL.search(sql) and _VARIABLE_VALUE_TOKEN.search(sql)):
        return None
    return (
        "Refused: summing 'variable_value' directly multi-counts. These EAV "
        "rows are overlapping strata (by gender, age, region, fishing technique, "
        "all under one variable_code), so a raw SUM/AVG double-counts people. Use "
        "the `aggregate` or `breakdown` tool grouped by ONE dimension (it scopes "
        "the code and excludes NULL keys), or ask for the number broken down by a "
        "single dimension. If neither fits, say the total cannot be computed "
        "reliably and do not report a raw sum."
    )


# The two fisheries landings tables. `fisheries_landings` holds area-level rows and
# `fisheries_landings_divisions` holds division-level rows. They describe the same catch at two
# granularities, and every division row is already part of its parent area row for the same year, so
# a SUM or AVG across both counts each division twice.
#
# The check works like `eav_magnitude_refusal`. A query is refused when a magnitude aggregate call
# and both table names appear anywhere in it, so a `UNION`, `JOIN` or subquery does not avoid it.
#
# `fisheries_landings` is a prefix of `fisheries_landings_divisions`, so the area-table pattern
# excludes a name followed by `_divisions`. A query that names only the divisions table, even twice,
# does not count as naming both.
_LANDINGS_TABLE_TOKEN = re.compile(
    r"\bfisheries_landings\b(?!_divisions)", re.IGNORECASE)
_LANDINGS_DIVISIONS_TABLE_TOKEN = re.compile(
    r"\bfisheries_landings_divisions\b", re.IGNORECASE)


def landings_double_count_refusal(sql: str) -> str | None:
    """Return a refusal message when `sql` applies a magnitude aggregate and names the two landings tables, and `None` otherwise.

    The magnitude aggregates are `SUM`, `AVG`, `MIN`, `MAX`, `STDDEV` and `MEDIAN`. A total has to come
    from one of the two tables.
    """
    if not _MAGNITUDE_AGG_CALL.search(sql):
        return None
    if not (
        _LANDINGS_TABLE_TOKEN.search(sql)
        and _LANDINGS_DIVISIONS_TABLE_TOKEN.search(sql)
    ):
        return None
    return (
        "Refused: 'fisheries_landings' (area-level) and "
        "'fisheries_landings_divisions' (division-level) are two "
        "granularities of the same catch, not two disjoint tables. Every "
        "division row already aggregates into its parent area row, so a "
        "SUM/AVG/MIN/MAX/STDDEV/MEDIAN spanning both double-counts. Totals "
        "must come from ONE of the two tables, not both."
    )


# Cube fallback. `query_data` runs on the primary SQL backend, which holds only some of the tables,
# while the DuckDB cube holds every dataset. When the primary reports a missing table that the cube
# has, the query runs on the cube. The cube names its tables with flat sanitized names
# (`hive.surimi.x` becomes `hive_surimi_x`), so the fully qualified names in the SQL are rewritten first.
_FQN = re.compile(r"\bhive\.(\w+)\.(\w+)\b", re.IGNORECASE)


def _default_sanitize(name: str) -> str:
    """Turn a fully qualified table name into a cube name by replacing dots with underscores and lowercasing.

    The server passes the cube's own sanitizer in place of this default.
    """
    return name.replace(".", "_").lower()


def _rewrite_fqns_to_cube(sql: str, cube_names, sanitize) -> tuple[str, bool]:
    """Rewrite each `hive.<schema>.<table>` in `sql` to the cube's flat table name in double quotes.

    A name whose sanitized form is not a cube dataset is left as it is. The function returns
    `(rewritten_sql, referenced)`, where `referenced` is True when at least one name matched a cube table.
    """
    names = set(cube_names)
    referenced = False

    def repl(match: "re.Match[str]") -> str:
        nonlocal referenced
        fqn = match.group(0)
        cube_name = sanitize(fqn)
        if cube_name in names:
            referenced = True
            return f'"{cube_name}"'
        return fqn

    return _FQN.sub(repl, sql), referenced


# Semantic views (see semantic_views.py) exist only as DuckDB views in the cube, under a bare name
# such as `v_sales_by_species_year` with no `hive.` prefix. A query on one of them has no qualified
# name for `_rewrite_fqns_to_cube` to match. This pattern finds an undotted relation name after `FROM`
# or `JOIN`, so the query can still be sent to the cube. The trailing `(?![\w.])` rejects a match
# that is followed by a word character or a dot, which leaves dotted names such as `hive.schema.table`
# to the qualified-name path.
_BARE_REL = re.compile(r'\b(?:from|join)\s+"?([A-Za-z_]\w*)"?(?![\w.])', re.IGNORECASE)


def _bare_relation_references_cube(sql: str, cube_names, sanitize) -> bool:
    """Return True when an undotted `FROM` or `JOIN` name in `sql` is a dataset that the cube lists.

    The name is sanitized in the same way as a qualified name, and a name that is not in `cube_names`
    does not count.
    """
    names = set(cube_names)
    return any(sanitize(m.group(1)) in names for m in _BARE_REL.finditer(sql))


_CUBE_BACKEND = "duckdb-cube"


def _cube_fallback(sql: str, limit: int, cube, sanitize) -> dict[str, Any] | None:
    """Run `sql` on the DuckDB cube and return the result dictionary, with the rewritten SQL under `_cube_sql`.

    The function returns `None` when the query names no cube table, either qualified or as a bare
    cube relation such as a semantic view, or when the cube raises an error. The caller then keeps
    the primary backend's error.
    """
    try:
        names = cube.datasets()
    except Exception:
        return None
    rewritten, referenced = _rewrite_fqns_to_cube(sql, names, sanitize)
    if not referenced:
        referenced = _bare_relation_references_cube(rewritten, names, sanitize)
    if not referenced:
        return None
    try:
        rows = cube.query(rewritten)
    except Exception:
        return None
    truncated = len(rows) > limit
    rows = rows[:limit]
    columns = list(rows[0].keys()) if rows else []
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "error": None,
        "_cube_sql": rewritten,
    }


class CubeFallbackClient:
    """A SQL client that runs on the primary backend and falls back to the DuckDB cube for a missing table.

    A result that the cube served carries `served_by="duckdb-cube"` and `cube_sql`, the rewritten
    SQL, so the signed function can record where the number came from. A result from the primary
    backend is returned unchanged.
    """

    def __init__(self, primary, cube, sanitize=None):
        self._primary = primary
        self._cube = cube
        self._sanitize = sanitize or _default_sanitize

    @property
    def backend_id(self) -> str | None:
        """The `backend_id` of the primary client, or None when it has none."""
        return getattr(self._primary, "backend_id", None)

    def execute(self, sql: str, limit: int = 500) -> dict[str, Any]:
        """Run `sql` on the primary client, and on the cube when the primary reports a missing table."""
        result = self._primary.execute(sql, limit=limit)
        err = str(result.get("error") or "")
        if err and _TABLE_MISS.search(err):
            fb = _cube_fallback(sql, limit, self._cube, self._sanitize)
            if fb is not None:
                fb["cube_sql"] = fb.pop("_cube_sql", None)
                fb["served_by"] = _CUBE_BACKEND
                return fb
        return result

    def describe_table(self, table: str) -> dict[str, Any]:
        """Describe `table` on the primary client."""
        return self._primary.describe_table(table)


def make_query_data_signed(
    client_factory: Callable[[], Any],
    backend_id: str = "trino://default",
) -> Callable[..., dict[str, Any]]:
    """Build a `query_data` function that runs SQL on the client and signs the result.

    The function calls `client_factory` on every call, so the connection is opened late. For a result
    that `CubeFallbackClient` served from the cube, the calculation record names the cube as the
    backend and holds the rewritten SQL in `human_check`. Any other result names the client's own
    `backend_id`, or the `backend_id` argument when the client has none.
    """

    @with_receipt("query_data")
    def query_data_signed(sql: str, limit: int = 500) -> dict[str, Any]:
        client = client_factory()
        result = client.execute(sql, limit=limit)

        # `CubeFallbackClient` adds these routing tags. They are removed here so that they stay out
        # of the signed value and appear only in the provenance.
        served_by = result.pop("served_by", None)
        cube_sql = result.pop("cube_sql", None)

        # The provenance names the backend that ran the query and carries a `human_check` block with
        # the SQL, so a reader can run it again. `with_receipt` moves the provenance into the signed record.
        if served_by:
            result["provenance"] = {
                **(result.get("provenance") or {}),
                "backend": served_by,
                "human_check": {
                    "sql": cube_sql or sql,   # the SQL as rewritten for the cube
                    "engine": served_by,
                    # The hash identifies the cube that ran the SQL.
                    "cube_sha256": lineage.get_cube_sha256(),
                    "source": "cube",
                    # A sentence on what the query did, for the person reading the answer.
                    "explanation": explain.explain_sql(sql),
                },
            }
        else:
            engine = getattr(client, "backend_id", None) or backend_id
            result["provenance"] = {
                **(result.get("provenance") or {}),
                "backend": engine,
                "human_check": {
                    "sql": sql,            # the SQL as it was sent
                    "engine": engine,
                    "cube_sha256": None,   # this path does not use the cube
                    "source": None,        # no source CSV is recorded on this path
                    "explanation": explain.explain_sql(sql),
                },
            }
        return result

    return query_data_signed
