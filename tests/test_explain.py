"""A receipt explains in words what was calculated and from which table (2026-10-14).

A person reading an answer needs the query and what it means, not the signature. The server writes the
explanation from the query it ran, its own disclosures and its dataset catalogue, never from the language
model, so it cannot invent a reason. Values and names the model chose are shown in code spans that no
markdown can escape: the chat renders this sentence as markdown under the answer.
"""
import re

import duckdb

from surimi_data_mcp.engine import aggregate as agg_mod
from surimi_data_mcp.engine.cube import Cube
from surimi_data_mcp.provenance import explain, lineage
from surimi_data_mcp.provenance.receipts import verify_receipt

CATCHES = "hive_eu_dcf_eu_dcf_catches"          # the cube name of catalogue dataset eu-dcf-catches


def test_sum_with_dimension_and_filter_reads_as_one_sentence():
    e = explain.explain_aggregate(
        "unknown_table_x", [{"col": "landings_t", "fn": "sum"}], ["year"],
        [{"col": "country_code", "op": "=", "value": "ESP"},
         {"col": "year", "op": ">=", "value": 2015}])
    assert e["summary"] == ("Sum of `landings_t` in table `unknown_table_x`, by `year`, "
                            "where `country_code` is `'ESP'` and `year` is at least 2015.")


def test_every_function_and_operator_has_words():
    e = explain.explain_aggregate(
        "t", [{"col": "a", "fn": "avg"}, {"col": "b", "fn": "count_distinct"}, {"col": "c", "fn": "count"},
              {"col": "d", "fn": "min"}, {"col": "e", "fn": "max"}], [],
        [{"col": "gear", "op": "in", "value": ["OTB", "PS"]},
         {"col": "area", "op": "not in", "value": ["27"]},
         {"col": "x", "op": "<>", "value": None}, {"col": "y", "op": "<", "value": 3},
         {"col": "z", "op": "<=", "value": True}, {"col": "w", "op": ">", "value": 1.5}])
    assert e["summary"] == (
        "Average of `a`, number of distinct values of `b`, number of non-empty values of `c`, "
        "lowest `d` and highest `e` in table `t`, "
        "where `gear` is one of `'OTB'`, `'PS'`, `area` is not one of `'27'`, `x` is not empty, "
        "`y` is below 3, `z` is at most true and `w` is above 1.5.")


def test_having_order_limit_and_truncation_are_stated():
    e = explain.explain_aggregate(
        "t", [{"col": "v", "fn": "sum"}], ["country"], [],
        having=[{"agg": "v_sum", "op": ">", "value": 10}], order=[{"col": "v_sum", "dir": "desc"}], limit=5,
        value={"truncated": True, "total_groups": 23})
    assert e["summary"] == ("Sum of `v` in table `t`, by `country`, keeping only groups where `v_sum` "
                            "is above 10, the top 5 of 23 by `v_sum`.")


def test_the_engine_disclosures_reach_the_reader():
    e = explain.explain_aggregate(
        "t", [{"col": "v", "fn": "sum"}], ["gear"], [{"col": "year", "op": "=", "value": 2022}],
        defaulted_year_col="year",
        value={"years_covered": "2013-2024", "suppressed": {"v": 41, "w": 12},
               "null_dimensions_excluded": {"gear": 7}, "normalized_dimensions": {"gear": {"otb": "OTB"}}})
    s = e["summary"]
    assert "`year` is 2022 (the latest year, chosen by default)" in s
    assert "Summed over every year in the table, 2013 to 2024." in s
    assert "This is a lower bound: 41 matching rows reported no value and are left out." in s
    assert "7 rows with no `gear` value are left out." in s
    assert "Spelling variants of `gear` were merged." in s


def test_a_catalogue_dataset_is_named_and_the_receipt_keeps_only_its_id():
    e = explain.explain_aggregate(CATCHES, [{"col": "landings_t", "fn": "sum"}], [], [])
    assert "in “EU DCF" in e["summary"]
    assert e["datasets"] == ["eu-dcf-catches"]


def test_dataset_notes_carry_the_catalogues_own_warning_and_column_descriptions():
    n = explain.dataset_notes("eu-dcf-catches")
    assert n["id"] == "eu-dcf-catches" and n["kind"] == "observed"
    assert "LOWER BOUND" in n["note"]
    medits = explain.dataset_notes("surimi-biomass-medits")
    assert "Geographical Sub-Area" in {c["name"]: c["description"] for c in medits["columns"]}["gsa_area"]
    assert explain.dataset_notes("no-such-dataset") is None


# --- what the model chose can never become markdown in the reader's view ---

HOSTILE = [
    "ESP ![x](https://evil.example/p.png) [Query and re-run](https://evil.example/)",
    "ESP\n2. Fake second line",
    "a ` ![x](https://evil.example/p.png) ` b",
    "``` <https://evil.example> ```",
    "<img src=x onerror=alert(1)>",
]


def _outside_code_spans(md: str) -> str:
    """The text of a markdown line with every code span removed (CommonMark: equal backtick runs)."""
    out, i = [], 0
    while i < len(md):
        if md[i] == "`":
            run = re.match(r"`+", md[i:]).group(0)
            j = i + len(run)
            close = -1
            while True:
                k = md.find(run, j)
                if k == -1:
                    break
                if md[k + len(run):k + len(run) + 1] != "`" and md[k - 1] != "`":
                    close = k
                    break
                j = k + 1
            if close == -1:
                out.append(run)
                i += len(run)
            else:
                i = close + len(run)
        else:
            out.append(md[i])
            i += 1
    return "".join(out)


def test_hostile_values_and_names_stay_inside_code_spans():
    for h in HOSTILE:
        for e in (explain.explain_aggregate("t", [{"col": h, "fn": "sum"}], [h], [{"col": h, "op": "=", "value": h}]),
                  explain.explain_aggregate(h, [{"col": "v", "fn": "sum"}], [], [{"col": "c", "op": "in", "value": [h]}])):
            s = e["summary"]
            assert "\n" not in s and "\r" not in s, s
            bare = _outside_code_spans(s)
            for bad in ("](", "![", "<", "evil"):
                assert bad not in bare, (h, s)


def test_hostile_table_names_in_sql_stay_inside_code_spans():
    for sql in ('WITH "a` ![x](https://evil.example/p.png) `b" AS (SELECT 1) SELECT * FROM "a` ![x](https://evil.example/p.png) `b"',
                'SELECT * FROM "x ![y](https://evil.example/z)"'):
        s = explain.explain_sql(sql)["summary"]
        bare = _outside_code_spans(s)
        assert "](" not in bare and "![" not in bare and "evil" not in bare, s


def test_sql_over_catalogue_tables_names_each_and_skips_ctes():
    e = explain.explain_sql(
        "WITH c AS (SELECT * FROM hive.eu_dcf.eu_dcf_catches) "
        "SELECT c.year, SUM(c.landings_t) FROM c JOIN hive.eu_dcf.fleet_economics f ON c.year = f.year GROUP BY 1")
    assert e["summary"].startswith("A read-only SQL query written by the assistant for this question, over ")
    assert set(e["datasets"]) == {"eu-dcf-catches", "eu-dcf-fleet-economics"}
    assert "table `c`" not in e["summary"]


def test_unparseable_sql_still_gets_a_plain_sentence():
    for sql in ("SELEC nonsense FROM", "", "SELECT (("):
        assert explain.explain_sql(sql) == {
            "summary": "A read-only SQL query written by the assistant for this question.", "datasets": []}


def _cube(tmp_path, monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", str(tmp_path / "absent.json"))
    monkeypatch.delenv("SURIMI_CUBE_DB", raising=False)
    lineage.reset()
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ds1 (country VARCHAR, year INTEGER, v INTEGER)")
    con.execute("INSERT INTO ds1 VALUES ('NO', 2020, 1), ('NO', 2021, 2), ('SE', 2021, 5)")
    return Cube(con)


def test_explanation_rides_inside_the_signed_receipt_once(tmp_path, monkeypatch):
    out = agg_mod.aggregate(_cube(tmp_path, monkeypatch), "ds1", [{"col": "v", "fn": "sum"}],
                            dimensions=["country"], filters=[{"col": "country", "op": "=", "value": "NO"}])
    hc = out["receipt"]["provenance"]["human_check"]
    assert hc["explanation"]["summary"] == (
        "Sum of `v` in table `ds1`, by `country`, where `country` is `'NO'`. "
        "Summed over every year in the table, 2020 to 2021.")
    # Once: the value the model reads does not carry a second copy.
    assert "explanation" not in out["value"]["provenance"]["human_check"]
    assert verify_receipt(out["receipt"], secret=b"test-secret")["verified"] is True
    edited = {**out["receipt"], "provenance": {**out["receipt"]["provenance"], "human_check": {
        **hc, "explanation": {**hc["explanation"], "summary": "Something else."}}}}
    assert verify_receipt(edited, secret=b"test-secret")["verified"] is False
    lineage.reset()


def test_a_defaulted_year_is_said_to_be_a_default(tmp_path, monkeypatch):
    out = agg_mod.aggregate(_cube(tmp_path, monkeypatch), "ds1", [{"col": "v", "fn": "sum"}],
                            filters=[{"col": "year", "op": "=", "value": 2021}], defaulted_year_col="year")
    s = out["receipt"]["provenance"]["human_check"]["explanation"]["summary"]
    assert "`year` is 2021 (the latest year, chosen by default)" in s
    lineage.reset()


def test_a_typical_figure_keeps_a_self_contained_audit_link(tmp_path, monkeypatch):
    """A link that needs the server's memory (#sig:) dies on a restart; a one-row total must not need it."""
    from surimi_data_mcp.provenance.audit_link import audit_url_for
    monkeypatch.setenv("SURIMI_PUBLIC_BASE_URL", "https://x.example")
    con = duckdb.connect(":memory:")
    con.execute(f"CREATE TABLE {CATCHES} (year INTEGER, country_code VARCHAR, landings_t DOUBLE)")
    con.execute(f"INSERT INTO {CATCHES} VALUES (2020, 'ESP', 10.5), (2021, 'ESP', 7)")
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", str(tmp_path / "absent.json"))
    lineage.reset()
    out = agg_mod.aggregate(Cube(con), CATCHES, [{"col": "landings_t", "fn": "sum"}],
                            filters=[{"col": "country_code", "op": "=", "value": "ESP"}])
    url = audit_url_for(out["receipt"])
    assert "#sig:" not in url, len(url)
    lineage.reset()


class _FakeClient:
    def execute(self, sql: str, limit: int = 500) -> dict:
        return {"rows": [{"n": 1}], "columns": ["n"], "row_count": 1, "error": None}


def test_query_data_receipt_explains_the_query(monkeypatch):
    from surimi_data_mcp.engine.sql_tools import make_query_data_signed

    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")
    qd = make_query_data_signed(lambda: _FakeClient(), backend_id="trino://test")
    out = qd("SELECT COUNT(*) FROM hive.eu_dcf.eu_dcf_catches")
    e = out["receipt"]["provenance"]["human_check"]["explanation"]
    assert e["summary"].startswith("A read-only SQL query written by the assistant for this question, over “")
    assert e["datasets"] == ["eu-dcf-catches"]
    assert verify_receipt(out["receipt"], secret=b"test-secret")["verified"] is True


def test_one_row_reads_in_the_singular():
    s = explain.explain_aggregate("t", [{"col": "v", "fn": "sum"}], ["g"], [],
                                  value={"suppressed": {"v": 1}, "null_dimensions_excluded": {"g": 1}})["summary"]
    assert "1 matching row reported no value and is left out." in s
    assert "1 row with no `g` value is left out." in s
