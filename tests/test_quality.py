"""Q-E11-4b: no SURIMI tool reports null rates, confidentiality suppression,
or year-coverage gaps. describe_quality gives, per column, a non-null count
and null fraction, the count of the confidentiality sentinel, and (for
categoricals) a distinct count -- plus, when a `year` column exists, the
years present/missing within the dataset's own span.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from surimi_data_mcp.engine.cube import build_cube
from surimi_data_mcp.datasets.quality import QualityError, describe_quality


@pytest.fixture
def cube(tmp_path: Path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "demo.csv").write_text(
        "year,country,measure_a,note\n"
        "2013,IT,10.5,ok\n"
        "2013,ES,C,confidential\n"
        "2014,IT,,missing\n"
        "2016,IT,12.0,ok\n",
        encoding="utf-8",
    )
    c = build_cube(str(root))
    yield c
    c.close()


def _col(out, name):
    return next(c for c in out["columns"] if c["name"] == name)


def test_unknown_dataset_raises():
    from surimi_data_mcp.engine.cube import build_cube as _bc
    empty = _bc(str(Path("/nonexistent-dir-for-test")))
    with pytest.raises(QualityError):
        describe_quality(empty, "nope")
    empty.close()


def test_row_count_and_dataset_name(cube):
    out = describe_quality(cube, "demo")
    assert out["dataset"] == "demo"
    assert out["row_count"] == 4


def test_non_null_count_and_null_fraction(cube):
    col = _col(describe_quality(cube, "demo"), "measure_a")
    assert col["non_null_count"] == 3          # one blank row
    assert col["null_fraction"] == pytest.approx(0.25)


def test_confidentiality_sentinel_is_counted(cube):
    col = _col(describe_quality(cube, "demo"), "measure_a")
    assert col["confidentiality_sentinel_count"] == 1


def test_a_column_with_no_sentinel_reports_zero(cube):
    col = _col(describe_quality(cube, "demo"), "country")
    assert col["confidentiality_sentinel_count"] == 0


def test_distinct_count_present_for_categorical_column(cube):
    col = _col(describe_quality(cube, "demo"), "country")
    assert col["distinct_count"] == 2          # IT, ES


def test_distinct_count_absent_for_a_numeric_year_column(cube):
    col = _col(describe_quality(cube, "demo"), "year")
    assert "distinct_count" not in col


def test_year_span_and_missing_year(cube):
    years = describe_quality(cube, "demo")["years"]
    assert years == {"first": 2013, "last": 2016, "missing": [2015]}


def test_years_is_none_without_a_year_column(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "noyear.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    c = build_cube(str(root))
    out = describe_quality(c, "noyear")
    assert out["years"] is None
    c.close()


# --- 2026-10-01: the string "NA" in a numeric column is a missing value -------

@pytest.fixture
def na_cube(tmp_path: Path):
    """An R-exported file: numeric columns hold the text NA for a missing value,
    so the whole column loads as text. `country` carries Namibia's real code NA."""
    root = tmp_path / "na"
    root.mkdir()
    (root / "ices.csv").write_text(
        "year,low_size,size,country\n"
        "2020,NA,10,IT\n"
        "2021,NA,12,NA\n"
        "2022,8.5,14,ES\n"
        "2023,NA,NA,IT\n",
        encoding="utf-8",
    )
    c = build_cube(str(root))
    yield c
    c.close()


def test_na_text_in_a_numeric_column_counts_as_missing(na_cube):
    """describe_quality used to report low_recruitment as 100% filled on an ICES
    table where about a third of the interval bounds are the text NA."""
    out = describe_quality(na_cube, "ices")
    low = _col(out, "low_size")
    assert low["na_text_count"] == 3
    assert low["na_counted_as_missing"] is True
    assert low["non_null_count"] == 1
    assert low["null_fraction"] == 0.75
    size = _col(out, "size")
    assert size["non_null_count"] == 3 and size["na_text_count"] == 1


def test_na_text_in_a_code_column_is_left_alone(na_cube):
    """NA is Namibia's country code: a column of codes is not numeric, so its NA
    cells are values, reported but not subtracted."""
    country = _col(describe_quality(na_cube, "ices"), "country")
    assert country["na_text_count"] == 1
    assert country["na_counted_as_missing"] is False
    assert country["non_null_count"] == 4


def test_a_column_without_na_text_has_no_na_fields(cube):
    year = _col(describe_quality(cube, "demo"), "year")
    assert "na_text_count" not in year
