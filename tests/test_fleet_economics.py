"""The EU DCF fleet-economics derivation.

Background. ``hive_eu_dcf_rev_grp.csv`` is an AUTH indicator file in LONG (EAV)
form: one row per AER variable, with the value copied into ``Revenue`` when the
variable is an income component and negated into ``GRP`` when it is a cost
component. Every other AER variable emits ``(0, 0)``. We registered it as a WIDE
table, so a single row is meaningless and only ``SUM(grp)`` happened to come out
right. ``SELECT DISTINCT`` over it destroys data, because two cost components
that share a value are two observations, not a repeat.

This module derives the same economics from the AER source instead, pivoted to
ONE ROW PER FLEET SEGMENT PER YEAR, with every measure named. The hand-checked
fixtures below are real AER values, copied from the lake file.

Definitions follow STECF's 2024 Annual Economic Report:

    total_income = landings_value + other_income + rights_income
                   + operating_subsidies
    gva          = total_income - energy_cost - repair_cost
                   - other_variable_cost - other_nonvariable_cost - rights_cost
    gross_profit = gva - personnel_cost - unpaid_labour_value
    net_profit   = gross_profit - depreciation

``gross_profit_excl_subsidies_and_rights`` narrows income to landings plus
other income, which is the basis AUTH's own Rev_GRP indicator uses. Keeping
both is what makes AUTH's file reconcilable against ours.
"""
from __future__ import annotations

import csv

import pytest

from surimi_data_mcp.loader.fleet_economics import (
    AER_MEASURES,
    DIMENSIONS,
    derive_fleet_economics,
    read_aer_rows,
)


def _write(path, rows, header=None):
    header = header or [
        "country_name", "country_code", "year", "supra_reg", "fishing_tech",
        "vessel_length", "geo_indicator", "cluster_name", "fs_name",
        "variable_group", "variable_name", "variable_code", "value", "unit",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return str(path)


def _seg(**kw):
    base = dict(
        country_name="Denmark", country_code="DNK", year="2008",
        supra_reg="AREA27", fishing_tech="PMP", vessel_length="VL1824",
        geo_indicator="NGI", cluster_name="NA", fs_name="DNK PMP",
        variable_group="", variable_name="", variable_code="", value="",
        unit="euro",
    )
    base.update(kw)
    return base


# Real AER rows for Denmark / 2008 / PMP / VL1824 / NGI, copied from
# "AER_economic_fleet segment.csv" in the SURIMI data lake.
DENMARK_2008_PMP = [
    ("Income",      "totlandginc",   "10114296.37"),
    ("Income",      "tototherinc",     "901163.58"),
    ("Income",      "totrightsinc",           "NA"),
    ("Income",      "totdirsub",               "0"),
    ("Expenditure", "totcrewwage",    "3503265.79"),
    ("Expenditure", "totenercost",     "1803232.2"),
    ("Expenditure", "totrepcost",     "1146685.86"),
    ("Expenditure", "totunpaidlab",   "1050891.79"),
    ("Expenditure", "totvarcost",      "924682.82"),
    ("Expenditure", "totrightscost",   "603934.24"),
    ("Expenditure", "totnovarcost",    "567845.34"),
    ("Expenditure", "totdepcost",     "2411392.02"),
    ("Capital",     "totdeprep",     "18643474.14"),
    ("Capital",     "totinvest",       "939428.51"),
    ("Employment",  "totjob",              "52.33"),
    ("Effort",      "tottrips",              "643"),
]

# Hand-computed from the rows above.
DENMARK_TOTAL_INCOME = 10114296.37 + 901163.58          # rights NA, subsidy 0
DENMARK_GVA = DENMARK_TOTAL_INCOME - (
    1803232.2 + 1146685.86 + 924682.82 + 567845.34 + 603934.24
)
DENMARK_GROSS_PROFIT = DENMARK_GVA - (3503265.79 + 1050891.79)
DENMARK_NET_PROFIT = DENMARK_GROSS_PROFIT - 2411392.02


@pytest.fixture()
def denmark_csv(tmp_path):
    rows = [_seg(variable_group=g, variable_code=c, value=v)
            for g, c, v in DENMARK_2008_PMP]
    return _write(tmp_path / "aer.csv", rows)


def test_one_row_per_segment_year(denmark_csv):
    """The whole point: the output is WIDE. Sixteen EAV rows collapse to one."""
    out = derive_fleet_economics(read_aer_rows(denmark_csv))
    assert len(out) == 1


def test_the_dimension_key_survives_the_pivot(denmark_csv):
    row = derive_fleet_economics(read_aer_rows(denmark_csv))[0]
    assert [row[d] for d in DIMENSIONS] == [
        "Denmark", "DNK", 2008, "AREA27", "PMP", "VL1824", "NGI",
    ]


def test_year_is_an_integer_not_a_string(denmark_csv):
    """A TEXT year broke ranking in the chat once already."""
    assert isinstance(derive_fleet_economics(read_aer_rows(denmark_csv))[0]["year"], int)


def test_every_declared_measure_is_present_on_every_row(denmark_csv):
    row = derive_fleet_economics(read_aer_rows(denmark_csv))[0]
    for measure in AER_MEASURES.values():
        assert measure in row, f"{measure} missing from the derived row"


def test_gross_profit_matches_the_stecf_formula(denmark_csv):
    row = derive_fleet_economics(read_aer_rows(denmark_csv))[0]
    assert row["gross_profit"] == pytest.approx(DENMARK_GROSS_PROFIT, abs=0.01)
    assert row["gross_profit"] == pytest.approx(1414921.91, abs=0.01)


def test_gva_and_total_income_and_net_profit(denmark_csv):
    row = derive_fleet_economics(read_aer_rows(denmark_csv))[0]
    assert row["total_income"] == pytest.approx(DENMARK_TOTAL_INCOME, abs=0.01)
    assert row["gva"] == pytest.approx(DENMARK_GVA, abs=0.01)
    assert row["net_profit"] == pytest.approx(DENMARK_NET_PROFIT, abs=0.01)


def test_landings_value_is_landings_alone_not_total_income(denmark_csv):
    """The rev_grp defect in one assertion.

    ``SUM(revenue)`` over the AUTH file returns total income, because every
    income component contributes a row. A user asking for fishing revenue means
    the gross value of landings.
    """
    row = derive_fleet_economics(read_aer_rows(denmark_csv))[0]
    assert row["landings_value"] == pytest.approx(10114296.37, abs=0.01)
    assert row["landings_value"] != pytest.approx(row["total_income"], abs=0.01)


def test_na_is_null_not_zero(denmark_csv):
    """``totrightsinc`` is NA here. NA is unknown; 0 is a measured zero."""
    row = derive_fleet_economics(read_aer_rows(denmark_csv))[0]
    assert row["rights_income"] is None
    assert row["operating_subsidies"] == 0.0


def test_a_null_component_does_not_poison_the_aggregate(denmark_csv):
    """NA must not make gross_profit itself null, or one gap voids a segment."""
    row = derive_fleet_economics(read_aer_rows(denmark_csv))[0]
    assert row["gross_profit"] is not None


def test_narrow_basis_reproduces_the_auth_definition(tmp_path):
    """Belgium 2008 TBB VL2440: AUTH's own SUM(GRP) is -3,076,839.55.

    It differs from the STECF figure by exactly the operating subsidy of
    883,380, which AUTH drops from income. Both numbers are defensible; serving
    one under the other's name is not.
    """
    rows = [
        _seg(country_name="Belgium", country_code="BEL", fishing_tech="TBB",
             vessel_length="VL2440", variable_group=g, variable_code=c, value=v)
        for g, c, v in [
            ("Income",      "totlandginc",  "54698472.45"),
            ("Income",      "tototherinc",      "1580388"),
            ("Income",      "totrightsinc",            "0"),
            ("Income",      "totdirsub",          "883380"),
            ("Expenditure", "totcrewwage",      "16574836"),
            ("Expenditure", "totenercost",      "25599503"),
            ("Expenditure", "totnovarcost",      "4227340"),
            ("Expenditure", "totrepcost",        "3214378"),
            ("Expenditure", "totrightscost",            "0"),
            ("Expenditure", "totunpaidlab",      "2076599"),
            ("Expenditure", "totvarcost",        "7663044"),
            ("Expenditure", "totdepcost",        "6767225"),
        ]
    ]
    row = derive_fleet_economics(read_aer_rows(_write(tmp_path / "be.csv", rows)))[0]
    assert row["gross_profit"] == pytest.approx(-2193459.55, abs=0.01)
    assert row["gross_profit_excl_subsidies_and_rights"] == pytest.approx(
        -3076839.55, abs=0.01
    )
    assert row["gross_profit"] - row["gross_profit_excl_subsidies_and_rights"] == (
        pytest.approx(883380.0, abs=0.01)
    )


def test_the_narrow_basis_also_drops_quota_leasing_income(tmp_path):
    """Denmark 2014 DTS VL1824 is why the column is not named for subsidies
    alone: AUTH drops ``totrightsinc`` (818,671.59) as well, and 704 of the
    12,022 segment-years turn on it.
    """
    rows = [
        _seg(year="2014", supra_reg="NAO", fishing_tech="DTS",
             variable_group=g, variable_code=c, value=v)
        for g, c, v in [
            ("Income",      "totlandginc",  "42363792.1876"),
            ("Income",      "tototherinc",     "845327.44"),
            ("Income",      "totrightsinc",  "818671.5941"),
            ("Income",      "totdirsub",                "0"),
            ("Expenditure", "totcrewwage", "10267679.8841"),
            ("Expenditure", "totenercost",  "6296598.1649"),
            ("Expenditure", "totvarcost",   "5323822.1012"),
            ("Expenditure", "totrepcost",   "4414086.6287"),
            ("Expenditure", "totunpaidlab", "3813786.0171"),
            ("Expenditure", "totnovarcost",  "2008780.249"),
            ("Expenditure", "totrightscost", "1980738.048"),
            ("Expenditure", "totdepcost",   "7651980.3348"),
        ]
    ]
    row = derive_fleet_economics(read_aer_rows(_write(tmp_path / "dk14.csv", rows)))[0]
    assert row["gross_profit"] == pytest.approx(9922300.13, abs=0.01)
    assert row["gross_profit_excl_subsidies_and_rights"] == pytest.approx(
        9103628.53, abs=0.01
    )


def test_activity_subsegments_are_summed_into_the_segment(tmp_path):
    """AER splits some segments by activity (A = active, L = low-active).

    ``activity`` is not a Rev_GRP dimension and is not one of ours. The two
    subsegments are real and must be ADDED, never deduplicated away.
    """
    rows = [
        _seg(country_name="Germany", country_code="DEU", year="2021",
             supra_reg="NAO", fishing_tech="PG", vessel_length="VL0008",
             variable_group="Income", variable_code="totlandginc",
             value=v, fs_name=fs)
        for v, fs in [("1000", "DEU PG A*"), ("250", "DEU PG L*")]
    ]
    out = derive_fleet_economics(read_aer_rows(_write(tmp_path / "de.csv", rows)))
    assert len(out) == 1
    assert out[0]["landings_value"] == pytest.approx(1250.0)


def test_a_segment_with_no_income_rows_still_appears(tmp_path):
    """Costs without income is a real state (inactive fleets). Do not drop it."""
    rows = [_seg(variable_group="Expenditure", variable_code="totenercost",
                 value="5000")]
    out = derive_fleet_economics(read_aer_rows(_write(tmp_path / "x.csv", rows)))
    assert len(out) == 1
    assert out[0]["gross_profit"] == pytest.approx(-5000.0)


def test_an_unknown_variable_code_is_ignored_not_crashed(tmp_path):
    rows = [
        _seg(variable_group="Income", variable_code="totlandginc", value="100"),
        _seg(variable_group="Somethingelse", variable_code="brand_new_code",
             value="999"),
    ]
    out = derive_fleet_economics(read_aer_rows(_write(tmp_path / "x.csv", rows)))
    assert out[0]["landings_value"] == pytest.approx(100.0)
    assert out[0]["gross_profit"] == pytest.approx(100.0)


def test_output_is_deterministic_in_row_order(denmark_csv):
    """The export is committed and diffed, so a stable order is load-bearing."""
    rows = read_aer_rows(denmark_csv)
    assert derive_fleet_economics(rows) == derive_fleet_economics(list(reversed(rows)))


def test_empty_input_yields_no_rows_not_a_crash(tmp_path):
    assert derive_fleet_economics(read_aer_rows(_write(tmp_path / "e.csv", []))) == []


def test_a_clustered_value_is_counted_once_not_per_cluster_member(tmp_path):
    """France 2010 overseas: two rows differing only in ``cluster_name``.

    ``fs_name`` is identical and ends in ``*``, which marks a value reported at
    cluster level and repeated on each member. Summing them doubled gross
    profit on 8 segment-years.
    """
    rows = [
        _seg(country_name="France", country_code="FRA", year="2010",
             supra_reg="OFR", fishing_tech="PGP", vessel_length="VL0010",
             geo_indicator="GP", fs_name="FRA OFR PGP0010 GP *",
             cluster_name=cl, variable_group="Income",
             variable_code="totlandginc", value="1448285")
        for cl in ("OFR PGP VL0010 GP L", "OFR PGP VL0010 GP A")
    ]
    out = derive_fleet_economics(read_aer_rows(_write(tmp_path / "fr.csv", rows)))
    assert len(out) == 1
    assert out[0]["landings_value"] == pytest.approx(1448285.0)


def test_distinct_fs_names_are_still_summed(tmp_path):
    """The other half of the rule, so the fix cannot silently drop real data."""
    rows = [
        _seg(fs_name=fs, variable_group="Income",
             variable_code="totlandginc", value=v)
        for fs, v in [("DNK PMP A*", "1000"), ("DNK PMP L*", "250")]
    ]
    out = derive_fleet_economics(read_aer_rows(_write(tmp_path / "dk.csv", rows)))
    assert out[0]["landings_value"] == pytest.approx(1250.0)


def test_a_real_value_beats_na_within_one_reported_segment(tmp_path):
    """29 (segment, fs_name, variable) groups hold a number and an NA."""
    rows = [
        _seg(fs_name="GRC DTS *", variable_group="Income",
             variable_code="totlandginc", value=v)
        for v in ("NA", "12351", "NA")
    ]
    out = derive_fleet_economics(read_aer_rows(_write(tmp_path / "gr.csv", rows)))
    assert out[0]["landings_value"] == pytest.approx(12351.0)


def test_a_segment_with_no_economics_at_all_is_null_not_zero(tmp_path):
    """4,733 of the 12,022 segment-years report no economic variable at all.

    Deriving 0.00 EUR for those puts "not reported" and "reported as zero" in
    the same bucket, which is how a fleet with no data reaches the top of a
    "lowest profit" ranking. Null keeps them out of SUM and out of the ranking.
    """
    rows = [_seg(variable_group="Income", variable_code=c, value="NA")
            for c in ("totlandginc", "tototherinc", "totdirsub")]
    out = derive_fleet_economics(read_aer_rows(_write(tmp_path / "n.csv", rows)))
    assert len(out) == 1, "the segment-year still exists and must be listed"
    for derived in ("total_income", "gva", "gross_profit",
                    "gross_profit_excl_subsidies_and_rights", "net_profit"):
        assert out[0][derived] is None, f"{derived} should be null, not 0.0"


def test_one_reported_component_is_enough_to_compute(tmp_path):
    """A measured zero is not missing data. One real number revives the row."""
    rows = [
        _seg(variable_group="Income", variable_code="totlandginc", value="NA"),
        _seg(variable_group="Income", variable_code="totdirsub", value="0"),
    ]
    out = derive_fleet_economics(read_aer_rows(_write(tmp_path / "z.csv", rows)))
    assert out[0]["gross_profit"] == pytest.approx(0.0)
    assert out[0]["operating_subsidies"] == pytest.approx(0.0)
