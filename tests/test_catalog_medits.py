"""Q-E5-2: the MEDITS survey CPUE registration.

The entry is `surimi-biomass-medits` in `datasets/datasets.yaml`, with table
`hive.surimi.medits_survey_cpue_mediterranean`. It keeps the raw file's shape (an unnamed
"col" index column, no NA-cleaning). Its other tests are in
tests/test_e6_medits_and_traits_registration.py.

The two tests below pin that the kg-versus-tonnes correction sits in `not_a_substitute_for`,
and that the `CSV_TO_TABLE` key is `hive_surimi_medits_survey_cpue_mediterranean.csv`, the
`hive_<schema>_<table>.csv` convention of the other served objects.
"""
from __future__ import annotations

from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.loader.load_csv import CSV_TO_TABLE


def test_medits_warning_rides_on_the_query_data_result():
    """catalog.substitute_warning() is what query_data attaches to its RESULT."""
    warning = catalog.substitute_warning("surimi-biomass-medits")
    assert warning, "surimi-biomass-medits should carry a not_a_substitute_for warning"
    low = warning.lower()
    assert "kg" in low and "km" in low


def test_medits_csv_uses_the_hive_prefixed_key_matching_the_served_bucket():
    """Every other CSV_TO_TABLE key follows hive_<schema>_<table>.csv,
    matching the objects under s3://project-surimi/mcp_data/tables/."""
    assert "hive_surimi_medits_survey_cpue_mediterranean.csv" in CSV_TO_TABLE
    assert CSV_TO_TABLE["hive_surimi_medits_survey_cpue_mediterranean.csv"] == \
        "surimi.medits_survey_cpue_mediterranean"
