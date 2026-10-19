"""Schema creation in the Postgres loader path.

FIX 2 (2026-09-02, coordinator-flagged from the rig load in
docs/reports/2026-09-02-datagui-landings-load.md): neither
``loader/load_csv.py``'s ``main()`` nor ``loader/bootstrap.py``'s
``_default_pg_loader`` created the `fisheries` schema -- both hardcoded a
fixed schema list (``"oecd"``, ``"eu_dcf"``, ``"surimi"``). The rig load had
to ``CREATE SCHEMA`` by hand; a real deploy would fail the same way the
first attempt did on this run.

Replaced with ``_ensure_schemas``, which derives the distinct schemas from
the actual batch of tables being loaded (a ``"schema.table"`` split), so a
new domain never needs a second hardcoded edit anywhere.

These tests pin the contract: exactly one ``CREATE SCHEMA IF NOT EXISTS``
per distinct schema, issued before any ``CREATE TABLE``, idempotent, and
never destructive (no DROP/ALTER against an existing schema). The file builds
the smallest spy cursor/connection needed to pin call order.
"""
from __future__ import annotations

from surimi_data_mcp.loader.load_csv import _ensure_schemas


class _SpyCursor:
    """Smallest fake DB cursor: records every SQL statement passed to
    execute()/executemany(), in call order, and does nothing else -- just
    enough for the real loader code to run without a live Postgres."""

    def __init__(self):
        self.calls: list[str] = []

    def execute(self, sql, params=None):
        self.calls.append(sql)

    def executemany(self, sql, batch):
        self.calls.append(sql)

    def close(self):
        pass


class _SpyConnection:
    def __init__(self, cursor: _SpyCursor):
        self._cursor = cursor
        self.autocommit = False

    def cursor(self):
        return self._cursor

    def close(self):
        pass


# ── _ensure_schemas: the extracted, directly-testable unit ──────────────────
def test_ensure_schemas_issues_exactly_one_create_per_distinct_schema():
    cur = _SpyCursor()
    _ensure_schemas(
        cur,
        [
            "fisheries.fisheries_landings",
            "fisheries.fisheries_landings_divisions",
            "oecd.fsedata",
        ],
    )
    assert cur.calls == [
        "CREATE SCHEMA IF NOT EXISTS fisheries",
        "CREATE SCHEMA IF NOT EXISTS oecd",
    ]


def test_ensure_schemas_is_idempotent_and_never_destructive():
    cur = _SpyCursor()
    _ensure_schemas(cur, ["surimi.a", "surimi.b", "eu_dcf.c"])
    # A second call (simulating a re-run) is still just IF NOT EXISTS --
    # never a DROP or ALTER against a schema that already has data.
    _ensure_schemas(cur, ["surimi.a", "surimi.b", "eu_dcf.c"])
    assert all(sql.startswith("CREATE SCHEMA IF NOT EXISTS") for sql in cur.calls)
    assert not any("DROP" in sql or "ALTER" in sql for sql in cur.calls)


def test_ensure_schemas_handles_an_empty_batch():
    cur = _SpyCursor()
    _ensure_schemas(cur, [])
    assert cur.calls == []


# ── main(): the real call sequence, spied end to end ─────────────────────────
def test_main_creates_every_schema_before_the_first_create_table(monkeypatch, tmp_path):
    """The regression this fix closes: a CREATE TABLE against a schema that
    was never created fails outright ("schema does not exist"). Pins schema
    creation strictly before any CREATE TABLE, for every distinct schema in
    the batch -- a two-schema, three-table fixture so a same-schema second
    table can't hide a dedup bug."""
    from surimi_data_mcp.loader import load_csv as load_csv_mod
    import psycopg2

    fixture_map = {
        "a1.csv": "schema_a.table1",
        "a2.csv": "schema_a.table2",
        "b1.csv": "schema_b.table1",
    }
    monkeypatch.setattr(load_csv_mod, "CSV_TO_TABLE", fixture_map)

    for fname in fixture_map:
        (tmp_path / fname).write_text("id,value\n1,5\n")
    monkeypatch.setenv("CSV_DIR", str(tmp_path))

    cur = _SpyCursor()
    conn = _SpyConnection(cur)
    monkeypatch.setattr(psycopg2, "connect", lambda url: conn)

    load_csv_mod.main()

    create_schema_calls = [c for c in cur.calls if c.startswith("CREATE SCHEMA")]
    create_table_calls = [c for c in cur.calls if c.startswith("CREATE TABLE")]

    assert create_schema_calls == [
        "CREATE SCHEMA IF NOT EXISTS schema_a",
        "CREATE SCHEMA IF NOT EXISTS schema_b",
    ], "expected exactly one CREATE SCHEMA per distinct schema, deduped"
    assert create_table_calls, "fixture should have reached CREATE TABLE at all"

    last_schema_index = max(cur.calls.index(c) for c in create_schema_calls)
    first_table_index = cur.calls.index(create_table_calls[0])
    assert last_schema_index < first_table_index, (
        "a CREATE SCHEMA ran after the first CREATE TABLE"
    )


# ── loader/bootstrap.py's _default_pg_loader: same contract, batch-scoped ───
def test_default_pg_loader_creates_only_the_batchs_schemas_before_create_table(
    monkeypatch, tmp_path
):
    """Same contract as main(), but the batch here is whatever csv_paths the
    caller passed in -- a schema for a table NOT in this batch (oecd.fsedata)
    must not be created, proving the fix is batch-scoped, not a second
    hardcoded universe."""
    from surimi_data_mcp.loader import bootstrap as bootstrap_mod
    from surimi_data_mcp.loader import load_csv as load_csv_mod
    import psycopg2

    fixture_map = {
        "x1.csv": "fisheries.fisheries_landings",
        "x2.csv": "fisheries.fisheries_landings_divisions",
        "y1.csv": "oecd.fsedata",  # deliberately NOT in this batch
    }
    monkeypatch.setattr(load_csv_mod, "CSV_TO_TABLE", fixture_map)

    batch_paths = []
    for fname in ("x1.csv", "x2.csv"):
        p = tmp_path / fname
        p.write_text("id,value\n1,5\n")
        batch_paths.append(p)

    cur = _SpyCursor()
    conn = _SpyConnection(cur)
    monkeypatch.setattr(psycopg2, "connect", lambda url: conn)

    bootstrap_mod._default_pg_loader(batch_paths, "postgresql://unused")

    create_schema_calls = [c for c in cur.calls if c.startswith("CREATE SCHEMA")]
    create_table_calls = [c for c in cur.calls if c.startswith("CREATE TABLE")]

    assert create_schema_calls == ["CREATE SCHEMA IF NOT EXISTS fisheries"], (
        "must create only the schema(s) this batch needs, not the whole "
        "CSV_TO_TABLE universe"
    )
    assert create_table_calls, "fixture should have reached CREATE TABLE at all"

    schema_index = cur.calls.index(create_schema_calls[0])
    first_table_index = cur.calls.index(create_table_calls[0])
    assert schema_index < first_table_index
