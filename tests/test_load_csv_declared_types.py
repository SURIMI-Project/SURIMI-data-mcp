"""Catalog-declared type wins over per-file inference for the landings tables.

FIX (2026-09-02, final review Important 5): ``list_columns
fisheries-landings-divisions`` returned ``fao_area`` as ``bigint`` while
``list_columns fisheries-landings`` returned it as ``text``, and both catalog
entries in ``datasets.yaml`` declare ``{"name": "fao_area", "type": "string"}``.
The cause is that ``_ColTypeInference`` runs per file: the area table carries
"27_NK" so its column stays TEXT, but the divisions file carries only "27"
so its column infers BIGINT. That is data-dependent (a future refresh that
puts one "_NK" value into the division-level file would silently flip the
column back to TEXT) and false against the catalog contract in the file
where it infers numeric.

The fix forces every catalog-declared ``string`` column to TEXT after
inference, overriding whatever the file's own values looked like. Uses the
same smallest-spy-cursor convention as
``tests/test_load_csv_schema_creation.py`` to pin the actual ``CREATE TABLE``
statement and the actual inserted (cast) values, not just the intermediate
type list.

CRITICAL REGRESSION FIX (2026-09-02, final-rereview.md finding 14): a first
version of this fix made the override generic (any table with a catalog
entry), on the premise that a catalog-declared ``string`` column is a no-op
for a table that is already typed correctly. That premise was false:
``datasets.yaml`` declares 26 genuinely NUMERIC columns as ``string`` across 7
PRE-EXISTING tables (``year``, ``value``, ``variable_value``, ``revenue``,
``grp``, ``total_gt`` and 20 more -- see
``docs/reports/2026-09-02-catalog-type-declarations.md`` in the monorepo for
the full list). The re-review proved end to end, loading a real
``eu_dcf.rev_grp`` fixture, that the generic override flipped ``revenue``
and ``year`` to TEXT and reproduced verbatim the two errors the type
inference exists to prevent: ``function sum(text) does not exist`` and
``operator does not exist: text = integer``. This plan's guarantee is
additive-only, so the override is now scoped to an explicit allowlist
(``_DECLARED_TYPE_OVERRIDE_TABLES``) containing only the two landings
tables it was built for. The tests below pin: the allowlist is exactly
those two names; the two landings tables still get the declared-TEXT
treatment; and a rev_grp-shaped fixture -- the re-reviewer's own scenario --
now keeps its numeric columns numeric through the real ``main()`` load path.
"""
from __future__ import annotations

from surimi_data_mcp.loader.load_csv import (
    _DECLARED_TYPE_OVERRIDE_TABLES,
    _declared_text_columns,
    _override_declared_types,
)


class _SpyCursor:
    def __init__(self):
        self.calls: list[str] = []
        self.executemany_calls: list[tuple[str, list]] = []

    def execute(self, sql, params=None):
        self.calls.append(sql)

    def executemany(self, sql, batch):
        self.calls.append(sql)
        self.executemany_calls.append((sql, batch))

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


# ── _declared_text_columns: the catalog lookup ───────────────────────────────

def test_declared_text_columns_includes_fao_area_for_both_landings_tables():
    for table in ("fisheries.fisheries_landings", "fisheries.fisheries_landings_divisions"):
        cols = _declared_text_columns(table)
        assert "fao_area" in cols
        assert "taxonomic_code" in cols
        # year is declared int, not string -- must not be forced to TEXT
        assert "year" not in cols
        # landings is declared float, not string -- must not be forced to TEXT
        assert "landings" not in cols


def test_declared_text_columns_empty_for_unknown_table():
    assert _declared_text_columns("no.such.table") == set()


def test_declared_text_columns_empty_for_non_allowlisted_table_with_string_declared_numerics():
    """`eu_dcf.aer_capacity` HAS a catalog entry that declares `year`,
    `number_of_vessels`, `total_vessel_power` and `total_vessel_tonnage` as
    "string" (they are genuinely numeric -- the wrong declaration
    `docs/reports/2026-09-02-catalog-type-declarations.md` documents), but it
    is not one of the two landings tables the override was built for, so
    `_declared_text_columns` must return nothing for it.

    This named `eu_dcf.rev_grp` until 2026-09-23. That table was dropped, and
    a name with no catalog entry made the test a duplicate of
    `test_declared_text_columns_empty_for_unknown_table` above: it passed for
    the wrong reason and guarded nothing. A live entry is what makes it a
    test of the ALLOWLIST rather than of the lookup miss."""
    assert _declared_text_columns("eu_dcf.aer_capacity") == set()


# ── the allowlist: pinned to exactly the two landings tables ────────────────

def test_declared_type_override_allowlist_is_exactly_the_two_landings_tables():
    """Extending this set requires first checking that the catalog's
    declared types are actually correct for the new table -- do not just
    add a name here (see the module docstring above and
    docs/reports/2026-09-02-catalog-type-declarations.md in the monorepo)."""
    assert _DECLARED_TYPE_OVERRIDE_TABLES == {
        "fisheries.fisheries_landings",
        "fisheries.fisheries_landings_divisions",
    }


# ── _override_declared_types: applies the catalog type after inference ───────

def test_override_forces_fao_area_to_text_even_when_inferred_bigint():
    headers = ["year", "fao_area", "landings"]
    inferred = ["BIGINT", "BIGINT", "DOUBLE PRECISION"]  # fao_area mis-inferred
    out = _override_declared_types(headers, inferred, "fisheries.fisheries_landings_divisions")
    assert out == ["BIGINT", "TEXT", "DOUBLE PRECISION"]


def test_override_leaves_non_catalog_table_untouched():
    headers = ["a", "b"]
    inferred = ["BIGINT", "TEXT"]
    out = _override_declared_types(headers, inferred, "no.such.table")
    assert out == inferred


# ── end-to-end: CREATE TABLE and the inserted value are both TEXT ───────────

def test_main_creates_fao_area_as_text_in_both_landings_tables(monkeypatch, tmp_path):
    from surimi_data_mcp.loader import load_csv as load_csv_mod
    import psycopg2

    fixture_map = {
        "area.csv": "fisheries.fisheries_landings",
        "divisions.csv": "fisheries.fisheries_landings_divisions",
    }
    monkeypatch.setattr(load_csv_mod, "CSV_TO_TABLE", fixture_map)

    # The area file carries a non-numeric fao_area value ("27_NK"); the
    # divisions file carries ONLY numeric-looking fao_area values ("27") --
    # the exact split that produced the bigint/text mismatch on the rig.
    # taxonomic_code is all-numeric-looking in BOTH files ("2.29E+11"),
    # which inference alone would coerce to DOUBLE PRECISION -- the
    # declared-string override must keep it TEXT in both tables too (b).
    (tmp_path / "area.csv").write_text(
        "year,fao_area,taxonomic_code,landings\n2020,27_NK,2.29E+11,10.5\n")
    (tmp_path / "divisions.csv").write_text(
        "year,fao_area,taxonomic_code,landings\n2020,27,2.29E+11,10.5\n")
    monkeypatch.setenv("CSV_DIR", str(tmp_path))

    cur = _SpyCursor()
    conn = _SpyConnection(cur)
    monkeypatch.setattr(psycopg2, "connect", lambda url: conn)

    load_csv_mod.main()

    create_table_calls = [c for c in cur.calls if c.startswith("CREATE TABLE")]
    assert len(create_table_calls) == 2
    for sql in create_table_calls:
        assert '"fao_area" TEXT' in sql, sql
        assert '"taxonomic_code" TEXT' in sql, sql

    # The divisions file's fao_area value ("27") is inserted as the STRING
    # "27", not the int 27 -- proving the cast (not just the column DDL)
    # respects the declared type.
    divisions_inserts = [
        batch for sql, batch in cur.executemany_calls
        if "fisheries_landings_divisions" in sql
    ]
    assert divisions_inserts
    row = divisions_inserts[0][0]
    fao_area_value = row[1]  # year, fao_area, taxonomic_code, landings
    taxonomic_code_value = row[2]
    assert fao_area_value == "27"
    assert isinstance(fao_area_value, str)
    assert taxonomic_code_value == "2.29E+11"
    assert isinstance(taxonomic_code_value, str)


# ── regression: the Critical the re-review caught (finding 14) ──────────────

def test_eav_shaped_fixture_keeps_numeric_columns_numeric_through_main(monkeypatch, tmp_path):
    """The re-reviewer's own reproduction, turned into a permanent regression
    test. The shape is the old `eu_dcf.rev_grp` (dropped 2026-09-23): a
    PRE-EXISTING table, not one of the two landings tables, whose catalog
    entry wrongly declared `year` and `revenue` as "string" (see
    docs/reports/2026-09-02-catalog-type-declarations.md). Before the
    allowlist fix, running this exact shape through `main()` created
    `year text, revenue text, grp text` and broke `SUM(revenue)` /
    `year IN (...)` with the two errors quoted in the re-review
    (`function sum(text) does not exist`, `operator does not exist: text =
    integer`). The allowlist must keep this table on BASE inference,
    byte-identical to pre-fix behaviour: `year` BIGINT, `revenue`/`grp`
    DOUBLE PRECISION, cast to Python int/float, not str. CSV_TO_TABLE is
    monkeypatched, so the fixture stands on its own and does not need the
    table to still be registered."""
    from surimi_data_mcp.loader import load_csv as load_csv_mod
    import psycopg2

    fixture_map = {"rev_grp.csv": "eu_dcf.rev_grp"}
    monkeypatch.setattr(load_csv_mod, "CSV_TO_TABLE", fixture_map)

    (tmp_path / "rev_grp.csv").write_text(
        "year,country_code,fishing_tech,revenue,grp\n"
        "2020,ESP,DFN,1234.5,987.6\n"
        "2021,ESP,DFN,2345.6,1098.7\n"
    )
    monkeypatch.setenv("CSV_DIR", str(tmp_path))

    cur = _SpyCursor()
    conn = _SpyConnection(cur)
    monkeypatch.setattr(psycopg2, "connect", lambda url: conn)

    load_csv_mod.main()

    create_table_calls = [c for c in cur.calls if c.startswith("CREATE TABLE")]
    assert len(create_table_calls) == 1
    sql = create_table_calls[0]
    assert '"year" BIGINT' in sql, sql
    assert '"revenue" DOUBLE PRECISION' in sql, sql
    assert '"grp" DOUBLE PRECISION' in sql, sql
    assert '"year" TEXT' not in sql
    assert '"revenue" TEXT' not in sql
    assert '"grp" TEXT' not in sql

    inserts = [batch for sql, batch in cur.executemany_calls if "rev_grp" in sql]
    assert inserts
    row = inserts[0][0]  # year, country_code, fishing_tech, revenue, grp
    assert row[0] == 2020 and isinstance(row[0], int)
    assert row[3] == 1234.5 and isinstance(row[3], float)
    assert row[4] == 987.6 and isinstance(row[4], float)
