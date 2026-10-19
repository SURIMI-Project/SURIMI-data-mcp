"""A refusal must not deny something the server now does.

Found live 2026-09-21. Asked in the deployed chat which datasets it could not
query, Muse answered that MEDITS and Traits.csv are "not registered in
catalog.py". Both ARE registered. The model was quoting a `cannot_answer`
detail written before the registration landed, and it preferred that hardcoded
sentence over the tool result sitting next to it.

This repo already learned that a capability declaration is not a guard, from
the other direction: a warning placed in a declaration went unread and had to
be moved onto the tool result. The same asymmetry cuts both ways. A declaration
the model DOES read is worse than useless when it is out of date, because it
overrides a correct answer with a confident wrong one.

Two claims in that entry were false and one was still true:

  false  "neither registered in catalog.py"  -- both are registered
  false  refusing "which species make up a functional group" -- the server
         has data_list_functional_groups, which returns the matched group and
         its species with FAO codes and is_fished
  true   MEDITS and Traits are not reachable for abundance questions on a
         deployment whose cube does not hold them

These tests pin the two corrections and keep the third.
"""
from __future__ import annotations

from surimi_data_mcp import capabilities
from surimi_data_mcp import server


def _details() -> str:
    return " ".join(e.detail for c in capabilities.all_capabilities()
                    for e in c.cannot_answer)


def _question_classes() -> str:
    return " ".join(e.question_class for c in capabilities.all_capabilities()
                    for e in c.cannot_answer)


def test_no_refusal_claims_medits_or_traits_are_unregistered():
    assert "neither registered in catalog.py" not in _details()


def test_a_refusal_that_names_them_says_what_is_actually_wrong():
    """Registered but not loaded is the true state, and it is the state the
    model should report, because it is what cube_servable already says."""
    d = _details()
    if "MEDITS" in d:
        assert "registered" in d
        assert "not loaded" in d or "cube" in d


def test_functional_group_membership_is_not_refused():
    """data_list_functional_groups answers it, so no entry may deny it."""
    assert "make up a functional group" not in _question_classes()


def test_the_refusal_points_at_the_tool_that_does_answer_it():
    assert "data_list_functional_groups" in _details()


def test_the_abundance_refusal_survives():
    """The half that is still true must not be lost in the correction."""
    q = _question_classes()
    assert "species abundance" in q or "abundance" in q
