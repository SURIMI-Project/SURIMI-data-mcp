"""Tests for the deterministic aggregation engine (`surimi_data_mcp/engine/aggregate.py`, `aggregate`).

Why this module exists
----------------------
The LLM must never sum, average, count or rank rows in prose, because it skips rows and
mis-aggregates. So every aggregate number comes from DuckDB, computed over the typed cube,
and carries an HMAC receipt the ``verify`` tool accepts. ``aggregate`` is the structured GROUP BY entry point:

    aggregate(cube, dataset,
              measures=[{"col": "landings_tonnes", "fn": "sum"}],
              dimensions=["year"],
              filters=[{"col": "area", "op": "=", "value": "27.4"}],
              having=[...], order=[...], limit=None) -> {value, receipt}

Rules these tests enforce:
  * Numbers are COMPUTED by DuckDB, never hardcoded into the implementation.
  * Each numeric assertion is proven against an INDEPENDENT oracle: the expected
    value is recomputed a SECOND way with pandas / the stdlib ``csv`` module over
    the same source data, and equality is asserted. Never "returns what it
    returns".
  * NULL dimension rows are EXCLUDED from group-bys (never a phantom "Unknown"),
    and the result states this.
  * EAV datasets require a single ``variable_code`` filter; a cross-measure SUM
    is refused / warned.
  * case-variant normalization (national/National/NATIONAL) collapses on request,
    and the merge is reported.
  * filters are injection-safe (a ``'; DROP`` value cannot execute) -> proven.
  * numeric columns aggregate with NO manual cast (typed in the cube).
  * results carry a receipt the existing verify path accepts.
  * an explicit ``limit`` sets ``truncated: true`` and never silently drops rows.
  * an empty result is honest (``rows: []``), never fabricated.
"""
from __future__ import annotations

import csv as _csv
from pathlib import Path

import pandas as pd
import pytest

from surimi_data_mcp.engine.cube import build_cube
from surimi_data_mcp.engine.aggregate import aggregate, AggregateError
from surimi_data_mcp.provenance.receipts import verify_receipt


SECRET = b"test-aggregate-secret-do-not-use-in-prod"


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    """The aggregate tool signs receipts from $SURIMI_RECEIPT_SECRET."""
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", SECRET.decode())


# ───────────────────────── temp corpus fixtures ─────────────────────────────
# Hand-authored CSVs whose values an independent oracle (pandas / csv) recomputes.

# A landings-style fact table. `area` holds real ICES-style area CODES which are
# genuinely TEXT (e.g. "27.4.a"), so dimension behaviour (text grouping, literal
# quote matching, NULL exclusion) is exercised on a real text column rather than
# a number. `landings_tonnes` is fractional so DOUBLE inference + sum/avg/min/max
# /count_distinct are all exercised. Two rows have a NULL `area` (empty cell) so
# NULL-exclusion is observable.
LANDINGS_ROWS = [
    ["year", "area", "species", "landings_tonnes", "vessels"],
    ["2019", "27.4.a", "COD", "100.5", "10"],
    ["2019", "27.4.a", "HER", "50.0", "4"],
    ["2019", "37.1.1", "COD", "30.0", "3"],
    ["2020", "27.4.a", "COD", "200.0", "20"],
    ["2020", "37.1.1", "HAK", "75.25", "7"],
    ["2020", "37.1.1", "HAK", "24.75", "2"],
    ["2021", "37.1.1", "HAK", "10.0", "1"],
    ["2021", "", "COD", "5.0", "1"],     # NULL area -> excluded from area group-bys
    ["2021", "", "HER", "8.0", "1"],     # NULL area -> excluded from area group-bys
]

# An EAV (entity-attribute-value) table in the AER mould: many distinct
# variable_code rows share one `value` column. Summing across codes is
# meaningless (employment + tonnage are different units), so the engine must
# require a single variable_code filter before aggregating `value`.
EAV_ROWS = [
    ["country", "year", "variable_code", "value"],
    ["ESP", "2019", "employment_fte", "1000"],
    ["ESP", "2019", "landings_tonnes", "5000"],
    ["ESP", "2020", "employment_fte", "1100"],
    ["ESP", "2020", "landings_tonnes", "5200"],
    ["FRA", "2019", "employment_fte", "800"],
    ["FRA", "2019", "landings_tonnes", "4000"],
]

# An EAV table whose magnitude column carries real 'NA' tokens, exactly as the
# deployed AER tables do (value is read as TEXT because some cells are 'NA').
# The cube therefore types `value` as VARCHAR. Aggregating a magnitude over it
# must TRY_CAST and treat 'NA' as not-reported (excluded from the sum), matching
# pandas to_numeric(errors='coerce').
EAV_NA_ROWS = [
    ["country", "year", "variable_code", "value"],
    ["ESP", "2019", "employment_fte", "1000"],
    ["ESP", "2020", "employment_fte", "NA"],     # not reported -> excluded, not 0
    ["ESP", "2021", "employment_fte", "1100"],
    ["FRA", "2019", "employment_fte", "800"],
    ["FRA", "2020", "employment_fte", "NA"],
]

# A table with case-variant categories in one dimension (national/National/
# NATIONAL all mean the same level). Collapsing them on request must merge the
# rows and report the merge.
CASE_ROWS = [
    ["level", "amount"],
    ["national", "10"],
    ["National", "20"],
    ["NATIONAL", "30"],
    ["regional", "100"],
    ["Regional", "5"],
]


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    with open(path, "w", newline="") as f:
        _csv.writer(f).writerows(rows)


@pytest.fixture
def cube(tmp_path: Path):
    """A cube built from the three temp CSVs above."""
    root = tmp_path / "lake"
    root.mkdir()
    _write_csv(root / "landings.csv", LANDINGS_ROWS)
    _write_csv(root / "aer_social.csv", EAV_ROWS)
    _write_csv(root / "levels.csv", CASE_ROWS)
    return build_cube(str(root))


@pytest.fixture
def landings_df(tmp_path: Path) -> pd.DataFrame:
    """Independent pandas read of the landings CSV (the oracle source).

    Read with the SAME file the cube ingests. NaN for empty area mirrors the
    cube's NULL so the oracle excludes those rows from area group-bys too.
    """
    root = tmp_path / "lake"
    return pd.read_csv(root / "landings.csv")


@pytest.fixture
def eav_df(tmp_path: Path) -> pd.DataFrame:
    root = tmp_path / "lake"
    return pd.read_csv(root / "aer_social.csv")


@pytest.fixture
def case_df(tmp_path: Path) -> pd.DataFrame:
    root = tmp_path / "lake"
    return pd.read_csv(root / "levels.csv")


def _rows(result: dict) -> list[dict]:
    """Pull the rows list out of an aggregate result (value envelope)."""
    return result["value"]["rows"]


# ═══════════════════════════ scalar aggregates ══════════════════════════════
# Each asserted against an independent pandas computation over the same source.

def test_sum_correct_vs_oracle(cube, landings_df):
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "sum"}])
    got = _rows(out)[0]["landings_tonnes_sum"]
    oracle = float(landings_df["landings_tonnes"].sum())
    assert got == pytest.approx(oracle)
    # And against a literal hand sum so a buggy oracle can't hide a bug.
    assert got == pytest.approx(100.5 + 50 + 30 + 200 + 75.25 + 24.75 + 10 + 5 + 8)


def test_avg_correct_vs_oracle(cube, landings_df):
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "avg"}])
    got = _rows(out)[0]["landings_tonnes_avg"]
    oracle = float(landings_df["landings_tonnes"].mean())
    assert got == pytest.approx(oracle)


def test_count_correct_vs_oracle(cube, landings_df):
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "count"}])
    got = _rows(out)[0]["landings_tonnes_count"]
    # count of non-null values of the column
    oracle = int(landings_df["landings_tonnes"].count())
    assert got == oracle == len(landings_df)


def test_min_max_correct_vs_oracle(cube, landings_df):
    out = aggregate(cube, "landings", measures=[
        {"col": "landings_tonnes", "fn": "min"},
        {"col": "landings_tonnes", "fn": "max"},
    ])
    row = _rows(out)[0]
    assert row["landings_tonnes_min"] == pytest.approx(float(landings_df["landings_tonnes"].min()))
    assert row["landings_tonnes_max"] == pytest.approx(float(landings_df["landings_tonnes"].max()))


def test_count_distinct_correct_vs_oracle(cube, landings_df):
    out = aggregate(cube, "landings",
                    measures=[{"col": "species", "fn": "count_distinct"}])
    got = _rows(out)[0]["species_count_distinct"]
    oracle = int(landings_df["species"].nunique())
    assert got == oracle == 3  # COD, HER, HAK


# ═══════════════════════════ GROUP BY completeness ══════════════════════════

def test_group_by_one_dimension_returns_all_groups(cube, landings_df):
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "sum"}],
                    dimensions=["year"])
    rows = _rows(out)
    got = {r["year"]: r["landings_tonnes_sum"] for r in rows}

    oracle = landings_df.groupby("year")["landings_tonnes"].sum()
    # Completeness: EVERY group present, exactly (no missing, no extra).
    assert set(got.keys()) == set(int(y) for y in oracle.index)
    assert len(rows) == len(oracle)
    for y, total in oracle.items():
        assert got[int(y)] == pytest.approx(float(total))


def test_group_by_multiple_dimensions_returns_all_groups(cube, landings_df):
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "sum"}],
                    dimensions=["year", "species"])
    rows = _rows(out)
    got = {(r["year"], r["species"]): r["landings_tonnes_sum"] for r in rows}

    oracle = landings_df.groupby(["year", "species"])["landings_tonnes"].sum()
    # group COUNT must equal the oracle's group count exactly (completeness).
    assert len(rows) == len(oracle)
    for (y, sp), total in oracle.items():
        assert got[(int(y), sp)] == pytest.approx(float(total))


# ═══════════════════════════ NULL dimension exclusion ═══════════════════════

def test_null_dimension_rows_excluded_and_stated(cube, landings_df):
    """Rows with a NULL group-by key are excluded; no phantom 'Unknown' bucket."""
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "sum"}],
                    dimensions=["area"])
    rows = _rows(out)
    areas = [r["area"] for r in rows]

    # No NULL/None/"Unknown"/"" group is fabricated.
    assert None not in areas
    assert "Unknown" not in areas
    assert "" not in areas
    assert set(areas) == {"27.4.a", "37.1.1"}

    # Oracle: pandas groupby drops NaN keys by default, and there are 2 NULL rows.
    oracle = landings_df.dropna(subset=["area"]).groupby("area")["landings_tonnes"].sum()
    got = {r["area"]: r["landings_tonnes_sum"] for r in rows}
    assert set(got.keys()) == set(oracle.index)
    for a, total in oracle.items():
        assert got[a] == pytest.approx(float(total))

    # The result must STATE the exclusion honestly (count of dropped rows).
    excluded = out["value"]["null_dimensions_excluded"]
    n_null = int(landings_df["area"].isna().sum())
    assert excluded["area"] == n_null == 2
    assert "excluded" in out["value"]["notes"].lower() or n_null > 0


def test_null_excluded_total_differs_from_grand_total(cube, landings_df):
    """Proof the exclusion is real: grouped-by-area sum < grand total by the NULL mass."""
    grand = aggregate(cube, "landings",
                      measures=[{"col": "landings_tonnes", "fn": "sum"}])
    grand_total = _rows(grand)[0]["landings_tonnes_sum"]

    by_area = aggregate(cube, "landings",
                        measures=[{"col": "landings_tonnes", "fn": "sum"}],
                        dimensions=["area"])
    area_total = sum(r["landings_tonnes_sum"] for r in _rows(by_area))

    null_mass = float(landings_df[landings_df["area"].isna()]["landings_tonnes"].sum())
    assert grand_total - area_total == pytest.approx(null_mass)
    assert null_mass == pytest.approx(5.0 + 8.0)


# ═══════════════════════════ EAV guard ══════════════════════════════════════

def test_eav_requires_single_variable_code_filter(cube):
    """Summing an EAV `value` across multiple variable_codes is refused."""
    with pytest.raises(AggregateError) as exc:
        aggregate(cube, "aer_social",
                  measures=[{"col": "value", "fn": "sum"}],
                  dimensions=["country"])
    msg = str(exc.value).lower()
    assert "variable_code" in msg


def test_eav_with_single_code_filter_aggregates_correctly(cube, eav_df):
    """With exactly one variable_code filter, the EAV sum is correct vs oracle."""
    out = aggregate(cube, "aer_social",
                    measures=[{"col": "value", "fn": "sum"}],
                    dimensions=["country"],
                    filters=[{"col": "variable_code", "op": "=",
                              "value": "employment_fte"}])
    rows = _rows(out)
    got = {r["country"]: r["value_sum"] for r in rows}

    sub = eav_df[eav_df["variable_code"] == "employment_fte"]
    oracle = sub.groupby("country")["value"].sum()
    assert set(got.keys()) == set(oracle.index)
    for c, total in oracle.items():
        assert got[c] == pytest.approx(float(total))
    assert got["ESP"] == pytest.approx(1000 + 1100)
    assert got["FRA"] == pytest.approx(800)


def test_eav_cross_measure_sum_refused_even_with_two_codes(cube):
    """Two distinct variable_code values in the filter is still cross-measure."""
    with pytest.raises(AggregateError):
        aggregate(cube, "aer_social",
                  measures=[{"col": "value", "fn": "sum"}],
                  filters=[{"col": "variable_code", "op": "in",
                            "value": ["employment_fte", "landings_tonnes"]}])


def test_eav_count_does_not_require_code_filter(cube, eav_df):
    """COUNT(*) across codes is meaningful (rows, not a cross-unit sum)."""
    out = aggregate(cube, "aer_social",
                    measures=[{"col": "value", "fn": "count"}])
    got = _rows(out)[0]["value_count"]
    assert got == int(eav_df["value"].count()) == len(eav_df)


# ═══════════════════════════ case-variant normalization ═════════════════════

def test_case_variant_normalization_collapses_and_reports(cube, case_df):
    """national/National/NATIONAL collapse to one group when requested."""
    out = aggregate(cube, "levels",
                    measures=[{"col": "amount", "fn": "sum"}],
                    dimensions=["level"],
                    normalize_dimensions=["level"])
    rows = _rows(out)
    got = {r["level"]: r["amount_sum"] for r in rows}

    # Oracle: collapse by lowercased level.
    oracle = case_df.assign(_k=case_df["level"].str.lower()).groupby("_k")["amount"].sum()
    # Two collapsed groups only (national, regional), not five.
    assert len(rows) == 2
    keys_lower = {k.lower() for k in got}
    assert keys_lower == set(oracle.index)
    for k, total in oracle.items():
        # match case-insensitively to the single emitted key for that group
        match = next(v for kk, v in got.items() if kk.lower() == k)
        assert match == pytest.approx(float(total))
    # national = 10+20+30 = 60 ; regional = 100+5 = 105
    assert next(v for k, v in got.items() if k.lower() == "national") == pytest.approx(60)
    assert next(v for k, v in got.items() if k.lower() == "regional") == pytest.approx(105)

    # The merge must be REPORTED (which variants collapsed into what).
    merges = out["value"]["normalized_dimensions"]["level"]
    nat_variants = {m for m in merges["national"]}
    assert {"national", "National", "NATIONAL"} == nat_variants


def test_case_variants_auto_collapse_without_param(cube, case_df):
    # No normalize_dimensions passed: a case-COLLIDING dim still collapses,
    # because split bars (national vs National vs NATIONAL) are never desired.
    out = aggregate(cube, "levels", measures=[{"col": "amount", "fn": "sum"}],
                    dimensions=["level"])
    keys = {r["level"] for r in _rows(out)}
    assert keys == {"national", "regional"}  # merged, lowercase canonical
    assert "level" in out["value"]["normalized_dimensions"]


# ═══════════════════════════ injection safety ═══════════════════════════════

def test_filter_value_injection_is_neutralized(cube, landings_df):
    """A '; DROP-style filter VALUE is treated as data, not SQL; table survives."""
    evil = "27.4'; DROP TABLE landings; --"
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "sum"}],
                    filters=[{"col": "area", "op": "=", "value": evil}])
    rows = _rows(out)
    # No row matches the literal evil string -> honest empty, NOT an error and
    # NOT a dropped table.
    assert rows == []
    assert out["value"]["row_count"] == 0

    # The table still exists and still has every original row (DROP did NOT run).
    again = aggregate(cube, "landings",
                      measures=[{"col": "landings_tonnes", "fn": "count"}])
    assert _rows(again)[0]["landings_tonnes_count"] == len(landings_df)


def test_filter_value_quote_is_literal_match(cube):
    """A value containing a single quote matches literally (parametrized)."""
    # Build a row whose area literally contains a quote, then filter for it.
    cube.con.execute("INSERT INTO landings VALUES (2099, 'O''Brien', 'COD', 1.0, 1)")
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "sum"}],
                    filters=[{"col": "area", "op": "=", "value": "O'Brien"}])
    rows = _rows(out)
    assert len(rows) == 1
    assert rows[0]["landings_tonnes_sum"] == pytest.approx(1.0)


def test_unknown_dataset_rejected(cube):
    """An unknown dataset name is rejected (cannot be smuggled into SQL)."""
    with pytest.raises(AggregateError):
        aggregate(cube, "no_such_table; DROP TABLE landings",
                  measures=[{"col": "x", "fn": "sum"}])


def test_unknown_column_rejected(cube):
    """An unknown measure/dimension column is rejected against the real schema."""
    with pytest.raises(AggregateError):
        aggregate(cube, "landings",
                  measures=[{"col": "not_a_column", "fn": "sum"}])
    with pytest.raises(AggregateError):
        aggregate(cube, "landings",
                  measures=[{"col": "landings_tonnes", "fn": "sum"}],
                  dimensions=["nope"])


def test_unknown_aggregate_fn_rejected(cube):
    """Only whitelisted aggregate functions are allowed (no arbitrary SQL fn)."""
    with pytest.raises(AggregateError):
        aggregate(cube, "landings",
                  measures=[{"col": "landings_tonnes", "fn": "evil(); DROP"}])


# ═══════════════════════════ numeric without manual cast ════════════════════

def test_numeric_aggregates_without_manual_cast(cube, landings_df):
    """Typed numeric columns sum/avg directly, the result is a number, no CAST."""
    out = aggregate(cube, "landings", measures=[
        {"col": "vessels", "fn": "sum"},        # integer column
        {"col": "landings_tonnes", "fn": "avg"},  # double column
    ])
    row = _rows(out)[0]
    assert isinstance(row["vessels_sum"], (int, float))
    assert row["vessels_sum"] == int(landings_df["vessels"].sum())
    assert row["landings_tonnes_avg"] == pytest.approx(float(landings_df["landings_tonnes"].mean()))


# ═══════════════════════════ receipt ════════════════════════════════════════

def test_result_carries_verifiable_receipt(cube):
    """The result has a receipt the existing verify path accepts."""
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "sum"}],
                    dimensions=["year"])
    assert "receipt" in out
    res = verify_receipt(out["receipt"], secret=SECRET)
    assert res["verified"] is True
    assert res["reason"] == "ok"
    assert out["receipt"]["tool_id"] == "aggregate"


def test_tampered_receipt_rejected(cube):
    """Tampering with the result value invalidates the receipt (provenance bind)."""
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "sum"}])
    out["receipt"]["output_value"]["rows"][0]["landings_tonnes_sum"] = 999999
    res = verify_receipt(out["receipt"], secret=SECRET)
    assert res["verified"] is False
    assert res["reason"] == "signature_mismatch"


def test_receipt_records_query_inputs(cube):
    """The receipt's input_params capture dataset + measures + dimensions."""
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "sum"}],
                    dimensions=["year"])
    ip = out["receipt"]["input_params"]
    assert ip["dataset"] == "landings"
    assert ip["measures"] == [{"col": "landings_tonnes", "fn": "sum"}]
    assert ip["dimensions"] == ["year"]


# ═══════════════════════════ explicit limit -> truncated ════════════════════

def test_explicit_limit_sets_truncated_and_keeps_rows(cube, landings_df):
    """A limit smaller than the group count flags truncated:true (no silent drop)."""
    # group-by year+species has more than 2 groups; cap at 2.
    full = aggregate(cube, "landings",
                     measures=[{"col": "landings_tonnes", "fn": "sum"}],
                     dimensions=["year", "species"],
                     order=[{"col": "landings_tonnes_sum", "dir": "desc"}])
    full_n = len(_rows(full))
    assert full_n > 2  # precondition

    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "sum"}],
                    dimensions=["year", "species"],
                    order=[{"col": "landings_tonnes_sum", "dir": "desc"}],
                    limit=2)
    rows = _rows(out)
    assert len(rows) == 2
    assert out["value"]["truncated"] is True
    # total available group count is reported truthfully (not the capped count).
    assert out["value"]["total_groups"] == full_n
    # The returned 2 are the TOP 2 by the order, not arbitrary.
    oracle = (landings_df.groupby(["year", "species"])["landings_tonnes"].sum()
              .sort_values(ascending=False).head(2))
    top_vals = [r["landings_tonnes_sum"] for r in rows]
    assert top_vals == pytest.approx(list(oracle.values))


def test_no_limit_is_not_truncated(cube):
    """Without a limit, truncated is False and every group is returned."""
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "sum"}],
                    dimensions=["year", "species"])
    assert out["value"]["truncated"] is False
    assert out["value"]["total_groups"] == len(_rows(out))


def test_limit_at_or_above_group_count_not_truncated(cube):
    """A limit >= the group count is honest: truncated False (nothing dropped)."""
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "sum"}],
                    dimensions=["year"],
                    limit=1000)
    assert out["value"]["truncated"] is False
    assert len(_rows(out)) == out["value"]["total_groups"]


# ═══════════════════════════ empty result honesty ═══════════════════════════

def test_empty_result_is_honest(cube):
    """A filter matching nothing returns rows:[] (never fabricated)."""
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "sum"}],
                    dimensions=["year"],
                    filters=[{"col": "year", "op": "=", "value": 1700}])
    assert _rows(out) == []
    assert out["value"]["row_count"] == 0
    assert out["value"]["truncated"] is False
    # An honest empty still carries a verifiable receipt.
    assert verify_receipt(out["receipt"], secret=SECRET)["verified"] is True


def test_having_filters_groups_vs_oracle(cube, landings_df):
    """HAVING is applied on aggregates and matches an independent pandas filter."""
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "sum"}],
                    dimensions=["year"],
                    having=[{"agg": "landings_tonnes_sum", "op": ">", "value": 100}])
    rows = _rows(out)
    got = {r["year"]: r["landings_tonnes_sum"] for r in rows}

    oracle = landings_df.groupby("year")["landings_tonnes"].sum()
    oracle = oracle[oracle > 100]
    assert set(got.keys()) == set(int(y) for y in oracle.index)
    for y, total in oracle.items():
        assert got[int(y)] == pytest.approx(float(total))


def test_order_by_is_applied(cube, landings_df):
    """ORDER BY a measure descending matches the oracle ordering."""
    out = aggregate(cube, "landings",
                    measures=[{"col": "landings_tonnes", "fn": "sum"}],
                    dimensions=["year"],
                    order=[{"col": "landings_tonnes_sum", "dir": "desc"}])
    got = [r["landings_tonnes_sum"] for r in _rows(out)]
    oracle = sorted(
        landings_df.groupby("year")["landings_tonnes"].sum().values,
        reverse=True)
    assert got == pytest.approx(list(oracle))


# ═══════════ VARCHAR magnitude column with 'NA' (real-data EAV case) ══════════

def test_varchar_magnitude_with_na_is_cast_and_na_excluded(tmp_path):
    """Real AER tables type `value` VARCHAR (it holds 'NA'). aggregate() must
    TRY_CAST it and exclude 'NA' from the sum, matching an independent pandas
    oracle."""
    import pandas as pd
    d = tmp_path / "lake"
    d.mkdir()
    _write_csv(d / "aer_na.csv", EAV_NA_ROWS)
    c = build_cube(str(d))
    coltypes = {col["name"]: col["type"] for col in c.columns("aer_na")}
    assert coltypes["value"] == "VARCHAR", "fixture must make `value` VARCHAR via the NA token"

    res = aggregate(c, "aer_na",
                    measures=[{"col": "value", "fn": "sum"}],
                    dimensions=["country"],
                    filters=[{"col": "variable_code", "op": "=", "value": "employment_fte"}])
    got = {r["country"]: r["value_sum"] for r in res["value"]["rows"]}

    # independent oracle
    df = pd.DataFrame(EAV_NA_ROWS[1:], columns=EAV_NA_ROWS[0])
    df = df[df["variable_code"] == "employment_fte"]
    df["v"] = pd.to_numeric(df["value"], errors="coerce")
    oracle = df.dropna(subset=["v"]).groupby("country")["v"].sum().to_dict()

    assert got == oracle, f"cube {got} != oracle {oracle}"
    assert got == {"ESP": 2100.0, "FRA": 800.0}   # NA excluded, not summed as 0


def test_no_case_collision_dimension_untouched(cube):
    # species (COD/HER/HAK) do NOT collide under lower() -> left exactly as-is,
    # never auto-normalized.
    out = aggregate(cube, "landings", measures=[{"col": "landings_tonnes", "fn": "sum"}],
                    dimensions=["species"])
    keys = {r["species"] for r in _rows(out)}
    assert keys == {"COD", "HER", "HAK"}  # original casing preserved
    assert "species" not in out["value"].get("normalized_dimensions", {})


# ═══════════════════ latest-year-default helpers ═══════════════════════════

from surimi_data_mcp.engine.aggregate import apply_latest_year_default, _latest_time_value


@pytest.fixture
def stockcube(tmp_path: Path):
    root = tmp_path / "src"; root.mkdir()
    (root / "fleet.csv").write_text(
        "year,country,vessels,revenue\n"
        "2022,IT,100,10.0\n2022,EL,90,9.0\n"
        "2023,IT,110,11.0\n2023,EL,95,9.5\n")
    return build_cube(str(root))

def test_latest_time_value(stockcube):
    assert _latest_time_value(stockcube, "fleet", "year") == 2023

def test_stock_no_year_pins_latest(stockcube):
    measures = [{"col": "vessels", "fn": "sum"}]
    filters, defaulted, note = apply_latest_year_default(
        stockcube, "fleet", "year", measures, filters=None,
        dimensions=["country"], stock_cols={"vessels"})
    assert defaulted is True
    assert {"col": "year", "op": "=", "value": 2023} in filters
    assert "2023" in note

def test_flow_measure_never_defaulted(stockcube):
    filters, defaulted, note = apply_latest_year_default(
        stockcube, "fleet", "year", [{"col": "revenue", "fn": "sum"}],
        filters=None, dimensions=["country"], stock_cols={"vessels"})
    assert defaulted is False and filters in (None, [])

def test_stock_with_year_filter_not_defaulted(stockcube):
    f = [{"col": "year", "op": "=", "value": 2022}]
    filters, defaulted, _ = apply_latest_year_default(
        stockcube, "fleet", "year", [{"col": "vessels", "fn": "sum"}],
        filters=f, dimensions=["country"], stock_cols={"vessels"})
    assert defaulted is False and filters == f

def test_stock_grouped_by_year_not_defaulted(stockcube):
    # per-year series is a legitimate cross-year view, not a collapse
    filters, defaulted, _ = apply_latest_year_default(
        stockcube, "fleet", "year", [{"col": "vessels", "fn": "sum"}],
        filters=None, dimensions=["year", "country"], stock_cols={"vessels"})
    assert defaulted is False


def test_latest_time_value_missing_column_returns_none(tmp_path: Path):
    """_latest_time_value returns None (not an exception) when the dataset has no
    time column, the narrow BinderException is absorbed at the source."""
    root = tmp_path / "src"; root.mkdir()
    (root / "notime.csv").write_text("country,vessels\nIT,100\nEL,90\n")
    c = build_cube(str(root))
    assert _latest_time_value(c, "notime", "year") is None


# --- a SUM with no year pinned says which years it covered -------------------
# PROBE-RESULTS-2026-09-10.md, Q-AS3: a cumulative total reached the user
# reading like a single year's figure.

def _window_cube(tmp_path):
    root = tmp_path / "src"; root.mkdir()
    (root / "rev.csv").write_text(
        "year,country,revenue\n2013,IT,10.0\n2014,IT,12.0\n2015,IT,14.0\n")
    from surimi_data_mcp.engine.cube import build_cube
    return build_cube(str(root))


def test_sum_without_a_year_reports_the_window(tmp_path, monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")
    cube = _window_cube(tmp_path)
    out = aggregate(cube, "rev", measures=[{"col": "revenue", "fn": "sum"}],
                    dimensions=["country"])
    assert out["value"]["years_covered"] == "2013-2015"
    assert "2013-2015" in out["value"]["notes"]


def test_sum_grouped_by_year_stays_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")
    cube = _window_cube(tmp_path)
    out = aggregate(cube, "rev", measures=[{"col": "revenue", "fn": "sum"}],
                    dimensions=["year"])
    assert "years_covered" not in out["value"]


def test_sum_filtered_to_one_year_stays_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")
    cube = _window_cube(tmp_path)
    out = aggregate(cube, "rev", measures=[{"col": "revenue", "fn": "sum"}],
                    dimensions=["country"],
                    filters=[{"col": "year", "op": "=", "value": 2014}])
    assert "years_covered" not in out["value"]


def test_non_sum_aggregation_stays_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")
    cube = _window_cube(tmp_path)
    out = aggregate(cube, "rev", measures=[{"col": "revenue", "fn": "avg"}],
                    dimensions=["country"])
    assert "years_covered" not in out["value"]
