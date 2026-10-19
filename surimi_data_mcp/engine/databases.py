"""SQL clients for the two backends the server can query, Trino and PostgreSQL.

`default_client` returns a `TrinoClient` unless `DB_BACKEND` is `postgres`, in which case it returns a
`PostgresClient` for `DATABASE_URL`. The PostgreSQL client rewrites table references such as
`hive.oecd.fsedata` to `oecd.fsedata` by dropping the catalog prefix.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional


class TrinoClient:
    """Runs SQL on a Trino server and returns rows as dictionaries."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8081,
        catalog: str = "hive",
        user: str = "surimi-mcp",
    ):
        self._host = host
        self._port = port
        self._catalog = catalog
        self._user = user

    @property
    def backend_id(self) -> str:
        """The Trino catalog, host and port, written the way a calculation record names its database."""
        return f"trino://{self._catalog}@{self._host}:{self._port}"

    def _connection(self):
        import trino
        return trino.dbapi.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            catalog=self._catalog,
        )

    def execute(self, sql: str, limit: int = 1000) -> dict[str, Any]:
        """Run `sql` on Trino and return `columns`, `rows`, `row_count`, `truncated` and `error`.

        `rows` is cut to `limit` and `truncated` says whether rows were dropped. On a failure `error`
        holds the message, and a message that mentions a column also carries `available_columns`.
        """
        try:
            conn = self._connection()
            cur = conn.cursor()
            cur.execute(sql)
            rows_raw = cur.fetchall()
            col_names = [desc[0] for desc in cur.description] if cur.description else []
            rows = [dict(zip(col_names, row)) for row in rows_raw]
            if len(rows) > limit:
                rows = rows[:limit]
            return {
                "columns": col_names,
                "rows": rows,
                "row_count": len(rows),
                "truncated": len(rows_raw) > limit,
                "error": None,
            }
        except Exception as e:
            result: dict[str, Any] = {
                "columns": [],
                "rows": [],
                "row_count": 0,
                "truncated": False,
                "error": str(e),
            }
            table_match = self._extract_table_from_sql(sql)
            if table_match and ("column" in str(e).lower()):
                schema_result = self.describe_table(table_match)
                if schema_result["error"] is None:
                    result["available_columns"] = schema_result["columns"]
            return result

    def describe_table(self, table: str) -> dict[str, Any]:
        """List the `name` and `type` of each column of `table` with `SHOW COLUMNS`, or return an `error`."""
        try:
            conn = self._connection()
            cur = conn.cursor()
            cur.execute(f"SHOW COLUMNS FROM {table}")
            rows = cur.fetchall()
            columns = [
                {"name": row[0], "type": row[1]}
                for row in rows
            ]
            return {"table": table, "columns": columns, "error": None}
        except Exception as e:
            return {"table": table, "columns": [], "error": str(e)}

    @staticmethod
    def _extract_table_from_sql(sql: str) -> Optional[str]:
        match = re.search(r'FROM\s+([\w."]+(?:\.[\w."]+)*)', sql, re.IGNORECASE)
        return match.group(1) if match else None


class PostgresClient:
    """Runs SQL on PostgreSQL with the same methods as `TrinoClient`."""

    def __init__(self, database_url: str):
        self._url = database_url

    @property
    def backend_id(self) -> str:
        """The host, port and database of `DATABASE_URL`, as a calculation record names its database.

        The user and password are left out because anyone who opens the record can read this string.
        """
        from urllib.parse import urlsplit
        u = urlsplit(self._url)
        port = f":{u.port}" if u.port else ""
        return f"postgresql://{u.hostname or 'localhost'}{port}{u.path}"

    # Second check behind `sql_tools.read_only_refusal`. Every transaction in the session is read-only,
    # so PostgreSQL itself refuses a write, and a statement is stopped after 60000 ms.
    _SESSION_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=60000"

    def _connection(self):
        import psycopg2
        return psycopg2.connect(self._url, options=self._SESSION_OPTIONS)

    def _pg_sql(self, sql: str) -> str:
        """Rewrite each `hive.<schema>.<table>` in `sql` to `<schema>.<table>`."""
        sql = re.sub(
            r'\bhive\.([\w]+)\.([\w]+)\b',
            r'\1.\2',
            sql,
        )
        return sql

    def execute(self, sql: str, limit: int = 1000) -> dict[str, Any]:
        """Run `sql` on PostgreSQL and return `columns`, `rows`, `row_count`, `truncated` and `error`.

        `rows` is cut to `limit` and `truncated` says whether rows were dropped. On a failure `error`
        holds the message, and a message that mentions a column also carries `available_columns`.
        """
        sql = self._pg_sql(sql)
        try:
            conn = self._connection()
            cur = conn.cursor()
            cur.execute(sql)
            rows_raw = cur.fetchall()
            col_names = [desc[0] for desc in cur.description] if cur.description else []
            rows = [dict(zip(col_names, row)) for row in rows_raw]
            if len(rows) > limit:
                rows = rows[:limit]
            conn.close()
            return {
                "columns": col_names,
                "rows": rows,
                "row_count": len(rows),
                "truncated": len(rows_raw) > limit,
                "error": None,
            }
        except Exception as e:
            result: dict[str, Any] = {
                "columns": [],
                "rows": [],
                "row_count": 0,
                "truncated": False,
                "error": str(e),
            }
            table_match = self._extract_table_from_sql(sql)
            if table_match and ("column" in str(e).lower()):
                schema_result = self.describe_table(table_match)
                if schema_result["error"] is None:
                    result["available_columns"] = schema_result["columns"]
            return result

    def describe_table(self, table: str) -> dict[str, Any]:
        """List the `name` and `type` of each column of `table` from `information_schema`, or return an `error`.

        A name with one dot is read as `schema.table`, one with two dots as `catalog.schema.table`,
        and a bare name is looked up in the `public` schema.
        """
        table = self._pg_sql(table)
        parts = table.replace('"', '').split(".")
        if len(parts) == 2:
            schema, tbl = parts
        elif len(parts) == 3:
            _, schema, tbl = parts
        else:
            schema, tbl = "public", parts[-1]

        try:
            conn = self._connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (schema, tbl),
            )
            rows = cur.fetchall()
            conn.close()
            columns = [{"name": row[0], "type": row[1]} for row in rows]
            if not columns:
                return {"table": table, "columns": [], "error": f"Table {table} not found"}
            return {"table": table, "columns": columns, "error": None}
        except Exception as e:
            return {"table": table, "columns": [], "error": str(e)}

    @staticmethod
    def _extract_table_from_sql(sql: str) -> Optional[str]:
        match = re.search(r'FROM\s+([\w."]+(?:\.[\w."]+)*)', sql, re.IGNORECASE)
        return match.group(1) if match else None


def default_client() -> TrinoClient | PostgresClient:
    """Build the client named by `DB_BACKEND`, which is `trino` unless set to `postgres`.

    Trino reads `TRINO_HOST`, `TRINO_PORT` and `TRINO_CATALOG`. PostgreSQL reads `DATABASE_URL`, which
    must be set: the code carries no default password.
    """
    backend = os.environ.get("DB_BACKEND", "trino").lower()
    if backend == "postgres":
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DB_BACKEND=postgres needs DATABASE_URL, for example "
                               "postgresql://surimi:<password>@localhost:26613/surimi")
        return PostgresClient(url)
    return TrinoClient(
        host=os.environ.get("TRINO_HOST", "127.0.0.1"),
        port=int(os.environ.get("TRINO_PORT", "8081")),
        catalog=os.environ.get("TRINO_CATALOG", "hive"),
    )
