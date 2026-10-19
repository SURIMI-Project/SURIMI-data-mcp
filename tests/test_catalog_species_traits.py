"""Q-E5-5: the FishBase/SeaLifeBase traits registration.

The entry is `fishbase-sealifebase-traits` in `datasets/datasets.yaml`, in the `ecological`
domain (external reference data, not SURIMI project output). Its table is
`hive.ecological.fishbase_sealifebase_traits` and its `CSV_TO_TABLE` key is
`hive_ecological_fishbase_sealifebase_traits.csv`. The other tests of the entry are in
tests/test_e6_medits_and_traits_registration.py.

Every column type is "string". `"NA"` is not in `_NONFINITE` in `loader/load_csv.py`, so it
makes `_ColTypeInference` read every numeric-looking column of the unmodified file as TEXT.
"""
from __future__ import annotations

from surimi_data_mcp.datasets import catalog
from surimi_data_mcp.loader.load_csv import CSV_TO_TABLE


def test_species_traits_warns_it_is_not_an_environmental_time_series():
    """catalog.substitute_warning() is what query_data attaches to its RESULT."""
    warning = catalog.substitute_warning("fishbase-sealifebase-traits")
    assert warning, "fishbase-sealifebase-traits should carry a not_a_substitute_for warning"
    low = warning.lower()
    assert "temperature" in low
    assert "time series" in low or "not a" in low


def test_species_traits_csv_uses_the_ecological_schema_object_name():
    """The object is hive_ecological_..., not hive_surimi_..."""
    assert "hive_ecological_fishbase_sealifebase_traits.csv" in CSV_TO_TABLE
    assert CSV_TO_TABLE["hive_ecological_fishbase_sealifebase_traits.csv"] == \
        "ecological.fishbase_sealifebase_traits"
    assert "hive_surimi_fishbase_sealifebase_traits.csv" not in CSV_TO_TABLE
