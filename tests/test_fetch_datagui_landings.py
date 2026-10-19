"""Tests for scripts/fetch_datagui_landings.py.

The tests cover normalise_row and UNION_COLUMNS, and write_union, which writes TWO csvs
into a dest dir instead of one union csv (see .superpowers/sdd/2026-09-01-datagui-landings-ingest/
revision-2.md, section R1), because the area-level and division-level ICES files
describe the same catch at two granularities and a single table would double count it.
"""
from scripts.fetch_datagui_landings import UNION_COLUMNS, _num, normalise_row


def test_the_misspelled_subdivision_column_is_mapped_not_duplicated():
    """2._DIVISION_LEVEL spells it `fao_subdivion`. Left alone that becomes a
    second near-identical column and the model picks whichever it saw first."""
    row = {"fao_subdivion": "27.7.d", "landings": "5", "year": "2016"}
    out = normalise_row(row, "atlantic_northeast_division_ices.csv")
    assert out["fao_subdivision"] == "27.7.d"
    assert "fao_subdivion" not in out


def test_missing_fao_unit_becomes_none_not_absent():
    """Only the area-level ICES file has fao_unit. A missing key would make the
    union ragged; the column must exist and be empty."""
    row = {"landings": "5", "year": "2016"}
    out = normalise_row(row, "mediterranean_and_black_sea.csv")
    assert "fao_unit" in out and out["fao_unit"] is None


def test_taxonomic_code_stays_a_string():
    """Upstream already lost precision (2.29E+11). Do not make it worse by
    parsing it as a number."""
    row = {"taxonomic_code": "2.29E+11", "landings": "1", "year": "2000"}
    assert normalise_row(row, "x.csv")["taxonomic_code"] == "2.29E+11"


def test_source_file_is_recorded_so_a_row_can_be_traced_back():
    row = {"landings": "1", "year": "2000"}
    out = normalise_row(row, "atlantic_northeast_ices.csv")
    assert out["source_file"] == "atlantic_northeast_ices.csv"


def test_landings_is_numeric_and_blank_becomes_none():
    assert normalise_row({"landings": "6143"}, "x.csv")["landings"] == 6143.0
    assert normalise_row({"landings": ""}, "x.csv")["landings"] is None
    assert normalise_row({"landings": "NA"}, "x.csv")["landings"] is None


def test_union_columns_are_snake_case_and_stable():
    assert UNION_COLUMNS == sorted(set(UNION_COLUMNS)), "must be deduped+sorted"
    assert all(c == c.lower() and " " not in c for c in UNION_COLUMNS)


def test_correct_spelling_wins_when_both_subdivision_keys_present():
    """A row carrying both the misspelled and the correct column must not
    depend on which one the CSV happened to order last. The canonical
    spelling always wins."""
    row = {
        "fao_subdivision": "27.7.d",
        "fao_subdivion": "WRONG",
        "landings": "5",
        "year": "2016",
    }
    out = normalise_row(row, "atlantic_northeast_division_ices.csv")
    assert out["fao_subdivision"] == "27.7.d"


def test_num_accepts_strict_thousands_separator():
    assert _num("1,234") == 1234.0
    assert _num("-1,234,567.5") == -1234567.5


def test_num_rejects_invalid_thousands_grouping():
    """Not a valid thousands form (only 2 digits in the last group) --
    commas must not be stripped blindly."""
    assert _num("1,23") is None


def test_num_zero_is_a_real_value_not_missing():
    assert _num("0") == 0.0


def test_write_union_streams_all_three_sources_read_only_into_two_files(tmp_path, capsys):
    """R1: the area file (fao37 + ices27 area-level) and the division file
    (ices27 division-level) describe overlapping catch at two granularities,
    so write_union splits them into two csvs instead of one union csv. This
    also asserts the client is only ever asked to READ: the bucket prefix
    belongs to someone else."""
    calls = []

    class FakeS3:
        def get_object(self, Bucket, Key, **kw):
            calls.append(("get_object", Key))
            body = ("landings,year,fao_area\n7,2001,37\n").encode()

            class B:
                def iter_lines(self, *a, **k):
                    for ln in body.splitlines():
                        yield ln

                def read(self):
                    return body

            return {"Body": B()}

    from scripts.fetch_datagui_landings import write_union

    counts = write_union(str(tmp_path), client=FakeS3())

    assert counts == {
        "hive_fisheries_fisheries_landings.csv": 2,
        "hive_fisheries_fisheries_landings_divisions.csv": 1,
    }
    assert all(c[0] == "get_object" for c in calls), "READ ONLY: no put/delete"
    assert len(calls) == 3

    landings_path = tmp_path / "hive_fisheries_fisheries_landings.csv"
    divisions_path = tmp_path / "hive_fisheries_fisheries_landings_divisions.csv"

    landings_lines = landings_path.read_text().splitlines()
    divisions_lines = divisions_path.read_text().splitlines()

    assert landings_lines[0].split(",") == UNION_COLUMNS
    assert divisions_lines[0].split(",") == UNION_COLUMNS

    # one row per source_file: 2 sources -> landings (fao37 + ices27 area),
    # 1 source -> divisions (ices27 division-level)
    assert len(landings_lines) - 1 == 2
    assert len(divisions_lines) - 1 == 1

    assert "WARNING" not in capsys.readouterr().out, "a clean run prints no warning"


def test_write_union_warns_on_unparseable_landings_and_still_writes_the_row(tmp_path, capsys):
    """A landings value that is present (not blank/NA) yet still fails to
    parse -- a formatting quirk beyond the thousands-separator case -- must
    not vanish silently. write_union counts it and prints one warning line
    per output file, and the row is still written, with landings empty."""

    class FakeS3:
        def get_object(self, Bucket, Key, **kw):
            if "Mediterranean" in Key:
                body = ("landings,year,fao_area\ngarbage,2001,37\n").encode()
            else:
                body = ("landings,year,fao_area\n7,2001,37\n").encode()

            class B:
                def iter_lines(self, *a, **k):
                    for ln in body.splitlines():
                        yield ln

                def read(self):
                    return body

            return {"Body": B()}

    from scripts.fetch_datagui_landings import write_union

    counts = write_union(str(tmp_path), client=FakeS3())
    out = capsys.readouterr().out

    assert "WARNING: hive_fisheries_fisheries_landings.csv: 1 landings values unparseable" in out
    assert "hive_fisheries_fisheries_landings_divisions.csv" not in out, "the divisions source was clean"

    landings_lines = (tmp_path / "hive_fisheries_fisheries_landings.csv").read_text().splitlines()
    assert counts["hive_fisheries_fisheries_landings.csv"] == 2, "the bad row is still written"
    landings_col = UNION_COLUMNS.index("landings")
    assert any(line.split(",")[landings_col] == "" for line in landings_lines[1:]), \
        "unparseable landings is written empty, not dropped"


def test_write_union_creates_dest_dir_if_missing(tmp_path):
    """write_union's docstring reads as though the function owns the output
    directory ("Stream the three sources into TWO csvs under `dest_dir`"),
    but open(..., "w") raises FileNotFoundError if dest_dir does not exist
    yet. A fresh checkout / a new export target has no such directory."""

    class FakeS3:
        def get_object(self, Bucket, Key, **kw):
            body = ("landings,year,fao_area\n7,2001,37\n").encode()

            class B:
                def iter_lines(self, *a, **k):
                    for ln in body.splitlines():
                        yield ln

                def read(self):
                    return body

            return {"Body": B()}

    from scripts.fetch_datagui_landings import write_union

    dest = tmp_path / "does" / "not" / "exist" / "yet"
    assert not dest.exists()

    counts = write_union(str(dest), client=FakeS3())

    assert dest.is_dir()
    assert (dest / "hive_fisheries_fisheries_landings.csv").exists()
    assert (dest / "hive_fisheries_fisheries_landings_divisions.csv").exists()
    assert counts["hive_fisheries_fisheries_landings.csv"] == 2
