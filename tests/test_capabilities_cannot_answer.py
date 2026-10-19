"""A server must be able to say what it cannot answer, and where it does live.

Measured on the 128-turn Muse replay of 2026-09-15: when an answer did not
exist the model said so 6 times and named where the data actually lives 0
times. Its abstentions were precise and sourced; they were blind. The Pipe's
system prompt already tells it to "name that place" when a tool result names
one, and no tool did. That is a tool-surface gap, not a prompt gap, which is
why this is a server field rather than more prompt wording.
"""
from surimi_data_mcp import capabilities


def _spec(**kw):
    base = dict(source_id="t", title="T", domain="d", description="x",
                coverage={}, tools=("a",))
    base.update(kw)
    return capabilities.DataSourceCapability(**base)


def test_a_capability_can_declare_what_it_cannot_answer():
    c = capabilities.CannotAnswer(
        question_class="which species a three-letter code refers to",
        why="not_served",
        detail="explain_indicator is keyed on column names; PIL is a value",
        lives_in="references/SURIMI-western_med/SURIMI-market_inputs/species.csv")
    spec = _spec(cannot_answer=(c,))
    assert spec.cannot_answer[0].lives_in.endswith("species.csv")


def test_cannot_answer_defaults_to_empty():
    """Existing registrations must keep working unchanged."""
    assert _spec().cannot_answer == ()


def test_why_is_constrained_to_the_three_kinds():
    """not_served, no_mechanism and not_collected need different responses.

    not_served is a plumbing fix. no_mechanism needs an engine nobody has
    written. not_collected needs data nobody gathered. Collapsing them loses
    the only distinction that tells a reader whether waiting will help.
    """
    for good in ("not_served", "no_mechanism", "not_collected"):
        capabilities.CannotAnswer(question_class="q", why=good, detail="d", lives_in="")
    try:
        capabilities.CannotAnswer(question_class="q", why="dunno", detail="d", lives_in="")
    except ValueError as e:
        assert "dunno" in str(e)
    else:
        raise AssertionError("an unknown `why` must be rejected")


def test_lives_in_may_be_empty_when_it_lives_nowhere():
    """A no_mechanism gap has no place to point at, and must not invent one."""
    c = capabilities.CannotAnswer(
        question_class="what revenue would be under a seasonal closure",
        why="no_mechanism", detail="no bioeconomic engine exists", lives_in="")
    assert c.lives_in == ""


def test_it_surfaces_through_describe_without_a_new_tool(monkeypatch):
    """No new tool: describe_data_capabilities already returns this dict."""
    spec = _spec(cannot_answer=(capabilities.CannotAnswer(
        question_class="q", why="not_served", detail="d", lives_in="some/file.csv"),))
    monkeypatch.setattr(capabilities, "_REGISTRY", [spec])
    out = capabilities.describe(["a"])
    entry = out["data_sources"][0]["cannot_answer"][0]
    assert entry["why"] == "not_served"
    assert entry["lives_in"] == "some/file.csv"


# ---------- what this server actually declares it cannot answer ----------

def _declared():
    """Every CannotAnswer the live server registers, flattened."""
    from surimi_data_mcp import server
    return [ca for c in capabilities.all_capabilities() for ca in c.cannot_answer]


def test_the_server_declares_the_gaps_the_reviewers_hit():
    """Four reviewers asked questions this server cannot answer today.

    Each entry must name where the answer DOES live, because that is the
    sentence the Pipe is already instructed to produce and could not.
    """
    declared = _declared()
    assert declared, "no cannot_answer entries registered"
    blob = " ".join(f"{d.question_class} {d.detail} {d.lives_in}" for d in declared)
    for expected in ("MEDITS", "Traits.csv", "EU_DCF_Catches", "species"):
        assert expected in blob, f"{expected} not declared anywhere"


# The folders some declared paths live in that a checkout of the monorepo may not have: the data
# lake, the exports and the NetCDF sample are gitignored, the partner repositories are separate
# clones or a submodule. A path under one of them is checked only where that folder is present, so
# CI (code without data) can run the suite. The folders are named here, never derived from the
# path; a path under any other folder is always checked.
_DATA_ROOTS = (
    "references/SURIMI-DATA-LAKE/",
    "references/SURIMI-protocol/",
    "references/SURIMI-western_med/",
    "data/exports/",
    "surimi-mcp/data/netcdf-samples/",
)


def _data_present(lives_in, *bases):
    import os
    root = next((r for r in _DATA_ROOTS if lives_in.startswith(r)), None)
    # An uninitialised submodule leaves an empty folder behind, so present means not empty.
    return root is None or any(os.path.isdir(os.path.join(b, root)) and os.listdir(os.path.join(b, root))
                               for b in bases)


def test_every_declared_gap_names_a_real_place_or_none_at_all():
    """lives_in is a quotable fact, so a path that does not exist is worse
    than an empty string. A no_mechanism gap must leave it empty."""
    import os
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for d in _declared():
        if d.why == "no_mechanism":
            assert d.lives_in == "", f"{d.question_class}: no_mechanism must not point anywhere"
            continue
        if not d.lives_in:
            continue
        # A declared path must resolve in the tree where it CAN resolve.
        # Two correct values cannot, in a fresh clone of the published repo:
        # references/ is the monorepo tree, and data/exports/ is the lake,
        # gitignored and present only where the data lives. So the assertion
        # is scoped to the monorepo, which is the one tree holding both.
        #
        # Why not derive a "checkable" parent and skip: data/exports/ carries
        # a trailing slash, so dirname() yields "data", which DOES exist in the
        # published repo. The skip then did not fire and a correct value
        # failed. The lesson is to test the declared string, not something
        # derived from it. Found by replaying this suite onto a fresh clone,
        # which is the only way this class of defect shows up.
        #
        # Paths contain spaces (the lake uses "1.3_EU DCF Catches"), so
        # lives_in is one whole path and must not be split on whitespace.
        monorepo = os.path.dirname(repo)
        if not os.path.isdir(os.path.join(monorepo, "references")):
            continue        # not the monorepo; nothing here to resolve against
        if not _data_present(d.lives_in, monorepo, repo):
            continue        # its data folder is not checked out here
        assert os.path.exists(os.path.join(monorepo, d.lives_in)) or \
            os.path.exists(os.path.join(repo, d.lives_in)), \
            f"{d.question_class}: lives_in does not resolve: {d.lives_in}"


def test_declared_gaps_use_only_the_three_reasons():
    assert {d.why for d in _declared()} <= set(capabilities._WHY)


def test_no_entry_claims_the_project_lacks_a_capability():
    """An abstention is a claim, and it reads as authoritative because it is specific.

    The model-mcp session shipped "No SURIMI experiment has been run, so there
    is no output to read" while a 74 MB run had existed for eighteen hours. The
    same failure was in this server: "SURIMI holds no bioeconomic engine that
    answers a counterfactual", when the project has a scenario-driven coupled
    stack (Ecopath, POSEIDON, market, fisheries-authority) behind a
    SubmitExperiment RPC.

    A cannot_answer entry may say what THIS SERVER does not serve. It may not
    say what SURIMI does not have, because this server cannot observe that and
    the answer changes without any code change here.
    """
    import re
    forbidden = (
        r"SURIMI holds no",
        r"SURIMI (?:has|does) not?\b",
        r"no SURIMI \w+ (?:has|have) been",
        r"\bno (?:bioeconomic |simulation |model )?engine\b",
        r"has (?:never|not) been run",
        r"there is no output",
    )
    for cap in capabilities.all_capabilities():
        for ca in cap.cannot_answer:
            text = f"{ca.question_class} {ca.detail}"
            for pat in forbidden:
                assert not re.search(pat, text, re.I), (
                    f"{cap.source_id}: entry asserts a project-wide absence "
                    f"this server cannot verify (matched {pat!r}). Scope the "
                    f"claim to what this server does not serve."
                )


def test_a_no_mechanism_entry_names_what_it_checked():
    """no_mechanism is the strongest claim available, so it carries the burden.

    "not_served" says a file is unregistered, which is checkable here.
    "no_mechanism" says the capability does not exist anywhere, which is a claim
    about the project on a given day. Any entry making it must name the evidence
    in lives_in, so the next reader can re-check it instead of trusting it.
    """
    for cap in capabilities.all_capabilities():
        for ca in cap.cannot_answer:
            if ca.why == "no_mechanism":
                assert ca.lives_in, (
                    f"{cap.source_id}: '{ca.question_class}' claims no mechanism "
                    f"exists but names no source. Either cite what was checked "
                    f"or downgrade to not_served."
                )


# ── naming the wrong source, not just the missing one ───────────────────────
# Root cause of the fabrication rate, across both lanes: 11 of 12 fabrications
# are a real, served, receipt-signed dataset offered as the answer to a question
# about something else. No number is invented; every figure reproduces. The
# failure is framing, and a generic "I cannot answer" does not stop it, because
# the model has a plausible table in hand and no reason to distrust it.
#
# What stops it is the server naming the substitution at the point the model
# would reach for it. Marking a table "modelled" says what the table IS; these
# entries say what NOT to answer from it.

def test_an_entry_forbids_answering_catches_from_simulated_sales():
    """Q-E9-1. The sharpest case: the receipt makes a wrong answer look verified."""
    from surimi_data_mcp import capabilities
    from surimi_data_mcp import server
    text = " ".join(f"{ca.question_class} {ca.detail}"
                    for cap in capabilities.all_capabilities()
                    for ca in cap.cannot_answer).lower()
    assert "surimi-sales-aegean-simulated" in text, \
        "the substitute table must be named, not just described"
    assert "catch" in text and "sales" in text, \
        "the entry must say sales are not catches"


def test_an_entry_says_no_record_exists_of_what_data_was_used_for():
    """Q-E10-4. Answered from architecture prose because nothing said otherwise."""
    from surimi_data_mcp import capabilities
    from surimi_data_mcp import server
    classes = [ca.question_class.lower()
               for cap in capabilities.all_capabilities()
               for ca in cap.cannot_answer]
    assert any("used for" in c or "what the data was used" in c for c in classes), classes


def test_every_declared_path_resolves():
    """An entry pointing somewhere plausible but wrong is worse than one pointing
    nowhere. Invariant borrowed from the model-mcp repo."""
    import os
    from surimi_data_mcp import capabilities
    from surimi_data_mcp import server
    repo = os.path.dirname(os.path.dirname(os.path.abspath(capabilities.__file__)))
    monorepo = os.path.dirname(repo)
    for cap in capabilities.all_capabilities():
        for ca in cap.cannot_answer:
            if not ca.lives_in:
                continue
            if not os.path.isdir(os.path.join(monorepo, "references")):
                continue            # not the monorepo; nothing to resolve against
            if not _data_present(ca.lives_in, monorepo, repo):
                continue            # its data folder is not checked out here
            assert (os.path.exists(os.path.join(monorepo, ca.lives_in))
                    or os.path.exists(os.path.join(repo, ca.lives_in))), \
                f"{ca.question_class}: lives_in does not resolve: {ca.lives_in}"


def test_a_no_mechanism_entry_names_nowhere():
    """no_mechanism means the capability exists nowhere, so there is no file to
    point at. A path on such an entry is a contradiction."""
    from surimi_data_mcp import capabilities
    from surimi_data_mcp import server
    for cap in capabilities.all_capabilities():
        for ca in cap.cannot_answer:
            if ca.why == "no_mechanism":
                assert not ca.lives_in, \
                    f"{ca.question_class}: no_mechanism must not name a location"
