"""Helpers that turn a table with one column per year (y_1994, y_1995, ...) into rows with a `year` column.

A table such as `surimi-biomass-medits` or `oecd-fsedata` has no `time_column` in the catalogue, so
`analyze_trend` and `compare_countries` fold its year columns into a view with these helpers before they run.
The helpers also find the years in which a selection holds no data.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

_YEAR = re.compile(r"^y_(\d{4})$")

#: The smallest number of y_YYYY columns that counts as a year series.
MIN_RUN = 3


def year_columns(columns: Iterable[str]) -> list[tuple[int, str]]:
    """Return the (year, column) pairs for the y_YYYY columns in `columns`, oldest first, or [] if there are fewer than `MIN_RUN`."""
    found = sorted((int(m.group(1)), c)
                   for c in columns if (m := _YEAR.match(str(c))))
    return found if len(found) >= MIN_RUN else []


def _q(name: str) -> str:
    """Return `name` as a double-quoted SQL identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def unpivot_sql(table: str, dimensions: Sequence[str],
                years: Sequence[tuple[int, str]], value_alias: str,
                where: str = "") -> str:
    """Return SQL that joins one SELECT per year column with UNION ALL, giving the `dimensions`, a `year` and the value as `value_alias`.

    `where` is added to each SELECT. It raises ValueError if `years` is empty.
    """
    if not years:
        raise ValueError("unpivot_sql needs at least one year column; "
                         "an empty list produced a bare CREATE VIEW and a "
                         "parser error.")
    dims = ", ".join(_q(d) for d in dimensions)
    lead = (dims + ", ") if dims else ""
    tail = f" WHERE {where}" if where else ""
    parts = [
        f'SELECT {lead}{year} AS "year", {_q(col)} AS {_q(value_alias)} '
        f"FROM {_q(table)}{tail}"
        for year, col in years
    ]
    return "\nUNION ALL\n".join(parts)


def empty_years(rows: Sequence[dict[str, Any]], year_key: str,
                value_key: str) -> list[int]:
    """Return the years in which every row's value under `value_key` is zero or null, oldest first.

    The caller reports these as years with no data and not as a change.
    """
    seen: dict[int, bool] = {}
    for r in rows:
        y = r.get(year_key)
        if y is None:
            continue
        v = r.get(value_key)
        seen[int(y)] = seen.get(int(y), False) or bool(v)
    return sorted(y for y, populated in seen.items() if not populated)


def cube_columns(cube, table: str) -> list[str]:
    """Return the column names of the cube table `table` in order, or [] if the lookup fails.

    The catalogue's column list can differ from the cube's. For `surimi-biomass-medits` it names a column `col` that the cube table does not have.
    """
    try:
        rows = cube.con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position", [table]).fetchall()
    except Exception:
        return []
    return [r[0] for r in rows]


def create_view_sql(view: str, body: str) -> str:
    """Return a CREATE OR REPLACE TEMP VIEW statement named `view` over the SQL `body`.

    The cube is opened with read_only=True and DuckDB refuses a CREATE in a read-only database, whereas a TEMP view lives in the writable temp database for the connection.
    """
    return f'CREATE OR REPLACE TEMP VIEW {_q(view)} AS {body}'


def measure_key(rows, year_key: str, dimensions) -> str | None:
    """Return the first numeric key of the first row that is not `year_key` or a dimension, or None if there are no rows or no such key.

    `run_analyze_trend` names the measure after its aggregate, for example `value_sum`, so the key is read from the rows.
    """
    if not rows:
        return None
    skip = {year_key} | {str(d) for d in (dimensions or [])}
    for k, v in rows[0].items():
        if k not in skip and isinstance(v, (int, float)):
            return k
    return None
