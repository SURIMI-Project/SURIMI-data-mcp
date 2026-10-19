"""Resolving a species or gear code to a name.

Two reviewers asked, four times across three forms, which species a
three-letter code stands for. One wrote: "what is the point of all answers if
you do not know about which species we are talking?" The server returned
found:false, because explain_indicator is keyed on COLUMN names and a code like
PIL is a VALUE inside species_code, so it could not match by construction.

The hazard, flagged by the model-mcp session after it hit the same thing on the
NetCDF species axis: a code is NOT a unique key. species.csv carries 90 rows and
86 distinct codes; ANE, HKE, MUT and PIL each appear twice, split by life stage.
Taking the first match would answer an adult-hake question with the juvenile row
and sign it.
"""
import pytest

from surimi_data_mcp.knowledge import lookups
from surimi_data_mcp.paths import APP_ROOT


def test_pil_resolves_to_its_scientific_name():
    """The code the reviewers actually asked about."""
    out = lookups.explain_species_code("PIL")
    assert out["found"] is True
    assert "Sardina pilchardus" in out["scientific_name"]


def test_an_ambiguous_code_reports_every_match_rather_than_picking_one():
    """PIL is two rows, adult and juvenile. Both are returned.

    The scientific name is the same for both, so the answer to "which species"
    is unambiguous even where the row is not. Silently taking one row would be
    a wrong answer in the cases where they differ.
    """
    out = lookups.explain_species_code("HKE")
    assert out["found"] is True
    stages = {m["life_stage"] for m in out["matches"]}
    assert stages == {"adult", "juvenile"}
    assert len(out["matches"]) == 2


def test_an_unambiguous_code_returns_exactly_one_match():
    out = lookups.explain_species_code("ALB")
    assert len(out["matches"]) == 1
    assert "Thunnus alalunga" in out["scientific_name"]


def test_lookup_is_case_insensitive():
    assert lookups.explain_species_code("pil")["found"] is True


def test_an_unknown_code_is_not_found_and_invents_nothing():
    out = lookups.explain_species_code("ZZZ")
    assert out["found"] is False
    assert out.get("scientific_name") is None


def test_the_answer_names_the_file_it_came_from():
    """A name with no source is not checkable, which is the whole complaint."""
    out = lookups.explain_species_code("PIL")
    assert "species.csv" in out["source"]


def test_a_gear_code_is_not_answered_from_the_species_vocabulary():
    """OTB and ART are gear codes, not species.

    The project files enumerate which gear codes exist (western_med_contract.yaml
    lists OTB, LLS, PS) but define none of them, and ART is undefined in every
    repo checked. Reporting "not found" is the correct answer until a gear
    vocabulary exists; guessing "otter trawl, bottom" would be unsourced.
    """
    for gear in ("OTB", "ART"):
        assert lookups.explain_species_code(gear)["found"] is False


def test_a_duplicated_code_is_never_resolved_to_a_single_row():
    """The hazard: available.index(value) takes the FIRST match.

    All four duplicated codes must return both rows, so no caller can mistake
    one life stage for the whole species.
    """
    for code in ("ANE", "HKE", "MUT", "PIL"):
        assert len(lookups.explain_species_code(code)["matches"]) == 2


def test_the_vocabulary_is_copied_into_the_image():
    """Being in the repo is not the same as being in the container.

    The test reads the Dockerfile, because that decides what the server can open.
    """
    dockerfile = APP_ROOT / "Dockerfile"
    assert dockerfile.exists(), f"no Dockerfile at {dockerfile}: this check must not pass by skipping"
    body = dockerfile.read_text(encoding="utf-8")
    assert "data/vocabularies/" in body, (
        "Dockerfile does not COPY data/vocabularies/, so species.csv will not "
        "exist in the image and every species code will return found:false")


def test_every_runtime_data_directory_is_copied_into_the_image():
    """The general form: any data/ subdir the server reads must be in the image.

    data/ holds the schema DDL and the lookup tables, and the Dockerfile copies
    each directory by name, so a new directory is missing from the image until
    someone queries production.
    """
    import os
    data_dir = APP_ROOT / "data"
    dockerfile = APP_ROOT / "Dockerfile"
    assert data_dir.is_dir() and dockerfile.exists(), "data/ and the Dockerfile must be found, not skipped"
    body = dockerfile.read_text(encoding="utf-8")
    runtime_dirs = {"vocabularies", "init"}   # read by the running server
    for name in sorted(os.listdir(data_dir)):
        if name in runtime_dirs and os.path.isdir(os.path.join(data_dir, name)):
            assert f"data/{name}/" in body, (
                f"data/{name}/ is read at runtime but never COPYed into the image")


def test_resolution_does_not_depend_on_a_parent_directory():
    """The answer must come from a path inside this repo."""
    out = lookups.explain_species_code("PIL")
    assert out["found"] is True
    assert not out["source"].startswith(".."), out["source"]


# ── fallback to the landings table ──────────────────────────────────────────
# The western-Med vocabulary covers 86 codes. hive.fisheries.fisheries_landings
# carries 1,306 distinct alpha3_code values with common_name beside each one, so
# a code the case study never mentions (COD, MAC, AAS) is still answerable.
#
# Checked before building this, 2026-09-17: of the 86 vocabulary codes, 61 appear
# in the landings table and 60 agree on the name. The single difference is SOO,
# "Solea spp" against "sole nei", which are the same thing. 25 vocabulary codes
# are ABSENT from the table, so the two sources are complementary and the
# vocabulary stays authoritative where both have an entry -- it carries the
# scientific name and the life-stage split, which the table does not.

@pytest.mark.live
@pytest.mark.live
def test_a_code_outside_the_westmed_vocabulary_still_resolves():
    """COD is in the landings table and not in the case-study vocabulary.

    Needs the real landings table. Without the live marker it passed only when an earlier test had
    left COD in the module's cache; tests/test_species_lookup_reaches_the_database.py covers the
    same path with a fake database."""
    out = lookups.explain_species_code("COD")
    assert out["found"] is True
    assert "cod" in (out.get("common_name") or "").lower()


def test_the_vocabulary_wins_where_both_sources_have_the_code():
    """PIL is in both. The vocabulary answer carries the scientific name.

    Preferring the table would lose Sardina pilchardus and the adult/juvenile
    split, which is a downgrade dressed as wider coverage.
    """
    out = lookups.explain_species_code("PIL")
    assert out["scientific_name"] == "Sardina pilchardus"
    assert len(out["matches"]) == 2


@pytest.mark.live
def test_the_fallback_says_which_source_answered():
    """A name with no source is not checkable, whichever source it came from."""
    out = lookups.explain_species_code("COD")
    assert "fisheries_landings" in out["source"]


def test_a_code_in_neither_source_is_still_honestly_not_found():
    out = lookups.explain_species_code("ZZZ")
    assert out["found"] is False


def test_the_fallback_never_raises_when_the_cube_is_unavailable(monkeypatch):
    """A chat tool call must degrade to not-found, never explode.

    _species_table_lookup reaches the cube, which is absent in some test and
    deployment contexts.
    """
    monkeypatch.setattr(lookups, "_species_table_lookup", lambda code: None)
    assert lookups.explain_species_code("COD")["found"] is False
    assert lookups.explain_species_code("PIL")["found"] is True   # vocab unaffected


def test_the_fallback_is_used_when_the_vocabulary_misses(monkeypatch):
    """The fallback branch, exercised without a backend.

    The two tests above need a live Trino, so CI (-m "not live") would otherwise
    never execute this path at all.
    """
    monkeypatch.setattr(lookups, "_species_table_lookup",
                        lambda code: {"common_name": "Atlantic cod",
                                      "scientific_name": "Gadus morhua"}
                        if code.upper() == "COD" else None)
    out = lookups.explain_species_code("COD")
    assert out["found"] is True
    assert out["common_name"] == "Atlantic cod"
    assert out["scientific_name"] == "Gadus morhua"   # scient_name, read too
    assert "fisheries_landings" in out["source"]


def test_the_fallback_does_not_override_the_vocabulary(monkeypatch):
    """Even if the table holds PIL, the vocabulary answer must win."""
    monkeypatch.setattr(lookups, "_species_table_lookup", lambda code: "WRONG")
    out = lookups.explain_species_code("PIL")
    assert out["scientific_name"] == "Sardina pilchardus"
    assert "WRONG" not in str(out)


# ── the landings table carries a scientific name too ────────────────────────
# The first version of the fallback selected only common_name and reported
# scientific_name: None with the note "this server holds no scientific name for
# it". That is false: fisheries_landings has a scient_name column beside
# common_name, populated (COD -> Gadus morhua, AAS -> Astacus astacus, verified
# live 2026-09-17). Claiming an absence that is not real is the same error as
# the false abstention in describe_data_capabilities, in a smaller place.
#
# Found by reading a benchmark transcript: the model answered a reviewer's
# species-code question from the table directly and cited scient_name, a column
# this tool was not reading.

def test_the_fallback_returns_the_scientific_name_too(monkeypatch):
    monkeypatch.setattr(lookups, "_species_table_lookup",
                        lambda code: {"common_name": "Atlantic cod",
                                      "scientific_name": "Gadus morhua"}
                        if code.upper() == "COD" else None)
    out = lookups.explain_species_code("COD")
    assert out["found"] is True
    assert out["common_name"] == "Atlantic cod"
    assert out["scientific_name"] == "Gadus morhua"


def test_the_fallback_never_claims_a_missing_scientific_name_it_has(monkeypatch):
    monkeypatch.setattr(lookups, "_species_table_lookup",
                        lambda code: {"common_name": "Atlantic cod",
                                      "scientific_name": "Gadus morhua"})
    note = lookups.explain_species_code("COD")["note"].lower()
    assert "no scientific name" not in note, note


def test_the_fallback_still_works_when_only_a_common_name_exists(monkeypatch):
    """scient_name is nullable; the common name alone is still an answer."""
    monkeypatch.setattr(lookups, "_species_table_lookup",
                        lambda code: {"common_name": "Sole nei",
                                      "scientific_name": ""})
    out = lookups.explain_species_code("COD")   # not in the westmed vocabulary
    assert out["found"] is True
    assert out["common_name"] == "Sole nei"
    assert out["scientific_name"] is None
