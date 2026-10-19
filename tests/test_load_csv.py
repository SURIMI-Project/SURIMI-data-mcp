"""Tests for CSV header sanitisation in the data loader."""
from surimi_data_mcp.loader.load_csv import _sanitize_header, _sanitize_headers


def test_dotted_headers_become_snake_case():
    assert _sanitize_header("total.trips") == "total_trips"
    assert _sanitize_header("vessel.length.category") == "vessel_length_category"
    assert _sanitize_header("supra.region") == "supra_region"


def test_spaces_and_mixed_case():
    assert _sanitize_header("Average Sea Days") == "average_sea_days"
    assert _sanitize_header("  Total  KW ") == "total_kw"


def test_already_clean_unchanged():
    assert _sanitize_header("year") == "year"
    assert _sanitize_header("country_code") == "country_code"
    assert _sanitize_header("supra_reg") == "supra_reg"


def test_collapses_and_trims_separators():
    assert _sanitize_header("a..b") == "a_b"
    assert _sanitize_header(".lead.trail.") == "lead_trail"
    assert _sanitize_header("") == "col"


def test_collision_disambiguation():
    # "total.trips" and "total_trips" both sanitise to total_trips -> suffix.
    assert _sanitize_headers(["total.trips", "total_trips"]) == ["total_trips", "total_trips_2"]


def test_headers_preserve_order_and_count():
    raw = ["year", "total.trips", "vessel.length.category", "country"]
    out = _sanitize_headers(raw)
    assert out == ["year", "total_trips", "vessel_length_category", "country"]
    assert len(out) == len(raw)


def test_explicit_rename_fromtable():
    # Run-together upstream name the generic rule cannot split; SURIMI
    # convention wants lower_snake_case words.
    assert _sanitize_header("fromtable") == "from_table"
    assert _sanitize_header("FromTable") == "from_table"


def test_bare_year_headers_get_y_prefix():
    # oecd.fsedata ships wide-format columns "2000".."2022": digit-leading
    # identifiers are invalid bare SQL and violate lower_snake_case.
    assert _sanitize_header("2000") == "y_2000"
    assert _sanitize_header("2022") == "y_2022"


def test_other_digit_leading_headers_get_col_prefix():
    assert _sanitize_header("22_06") == "col_22_06"


def test_year_prefix_collision_disambiguated():
    assert _sanitize_headers(["2000", "y_2000"]) == ["y_2000", "y_2000_2"]
