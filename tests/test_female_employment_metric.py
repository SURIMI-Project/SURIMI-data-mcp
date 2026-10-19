"""female_employment counts women, not every socemploy row (2026-10-13).

The AER social table repeats the same people under several breakdowns: by gender, by employment
status, by age and so on. The metric summed variable_value over all socemploy rows, so with no
filter it added the men, then the same workforce again by status, and labelled the result "Women
employed". It now binds gender to Female itself, so the rows of the other breakdowns never enter.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from surimi_data_mcp.datasets.metrics import REGISTRY, catalog_prompt, run_query_metric, validate_registry
from surimi_data_mcp.engine.cube import build_cube


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-female-employment-secret")


@pytest.fixture
def social(tmp_path: Path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "social.csv").write_text(
        "year,country_name,country_code,gender,employment_status,variable_code,variable_value\n"
        "2020,Spain,ESP,Female,,socemploy,10\n"
        "2020,Spain,ESP,Male,,socemploy,90\n"
        "2020,Spain,ESP,,Owner,socemploy,40\n"
        "2020,Spain,ESP,,Employee,socemploy,60\n"
        "2020,Spain,ESP,Female,,socfte,7\n")
    cube = build_cube(str(root))
    metric = replace(next(m for m in REGISTRY if m.name == "female_employment"), dataset="social")
    return cube, metric


def test_the_metric_counts_only_the_women(social):
    cube, metric = social
    out = run_query_metric(cube, "female_employment", registry=(metric,))
    rows = out["value"]["rows"]
    assert len(rows) == 1
    assert list(rows[0].values())[-1] == 10


def test_the_metric_is_a_headcount(social):
    _, metric = social
    assert metric.unit == "persons"


def test_the_metric_validates_against_its_table(social):
    cube, metric = social
    validate_registry((metric,), cube)


def test_the_metric_list_says_what_it_binds():
    line = next(l for l in catalog_prompt().splitlines() if l.startswith("- female_employment"))
    assert "gender" in line and "Female" in line
