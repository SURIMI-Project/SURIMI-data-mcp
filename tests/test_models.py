"""Tests for the model knowledge base (describe_models capability).

Guards the things that made the chat untrustworthy on model/architecture questions:
(1) no fabricated provenance (POSEIDON=UOXF not HCMR; CMSY=Catch-MSY),
(2) a miss reads as "not documented here", never "not used in SURIMI",
(3) the coupling story is grounded (hub-and-spoke Controller, not "EwE resolves POSEIDON"),
(4) every entry carries grounded `sources`.
"""
from surimi_data_mcp.knowledge import models


def test_lists_models_and_runtime_components():
    out = models.describe_models()
    ids = {m["id"] for m in out["models"]}
    # scientific models
    assert {"ewe", "cmsy", "poseidon", "mice", "system-dynamics", "value-chain",
            "nlp-assessment", "nlp-policy", "osmose"} <= ids
    # runtime / interoperability components
    assert {"controller", "output-creator", "fishery-authority", "market",
            "environment", "gui", "interoperability-layer"} <= ids
    assert out["count"] == len(models.MODELS)
    assert "Controller" in out["surimi_architecture"] or "hub-and-spoke" in out["surimi_architecture"]


def test_find_by_synonyms_models_and_components():
    assert models.find_model("EwE")["id"] == "ewe"
    assert models.find_model("Ecopath")["id"] == "ewe"
    assert models.find_model("CMSY++")["id"] == "cmsy"
    assert models.find_model("the controller")["id"] == "controller"
    assert models.find_model("fisheries authority")["id"] == "fishery-authority"
    assert models.find_model("value chain")["id"] == "value-chain"
    assert models.find_model("aggregator")["id"] == "output-creator"
    assert models.find_model("nlp policy")["id"] == "nlp-policy"
    assert models.find_model("interoperability layer")["id"] == "interoperability-layer"


def test_poseidon_is_uoxf_not_hcmr_and_dual_deliverable():
    m = models.describe_models("POSEIDON")["model"]
    # grounded in the consortium: UOXF/Oxford + the actual maintainer (Payette)
    assert "Oxford" in m["developer"] or "UOXF" in m["developer"]
    assert "Payette" in m["developer"]
    # HCMR appears only as a flagged-unrelated disambiguation, never as the origin
    assert "HCMR" in (m["disambiguation"] or "")
    assert "unrelated" in (m["disambiguation"] or "")
    # POSEIDON is D3.3 (operating model) + optional D3.8 (policy), not only D3.8
    assert "D3.3" in m["surimi_role"] and "D3.8" in m["surimi_role"]
    # grounded in the repo: Java/Gradle server wrapping POSEIDON, Western Med case study
    assert "SURIMI-POSEIDON" in m["surimi_role"] and "western_med" in m["surimi_role"].lower()
    # the fabrication killer: EwE does NOT internally resolve POSEIDON -> Controller
    assert "Controller" in m["surimi_role"]
    assert "not resolve each other internally" in m["surimi_role"] or "do NOT resolve each other internally" in m["surimi_role"]


def test_cmsy_is_catch_msy_not_invented_acronym():
    m = models.describe_models("CMSY++")["model"]
    assert "Catch-MSY" in m["developer"] or "Catch-MSY" in m["what_it_is"]
    assert "not 'Catch-Multiple-Standard-Yield'" in m["developer"]
    assert "Froese" in m["developer"]
    assert "D3.6" in m["surimi_role"]


def test_cmsy_explains_the_catch_driven_default_biomass_prior():
    """Q-E3-10: a run started with no expert stb.low/endb.low prior (every
    GSA06 input stock in id_file.csv leaves both blank) falls back to
    CMSY++'s catch-driven default, which forces the depletion prior into a
    fixed range once early/late catch crosses a threshold of MSY. Without
    this the chat has no way to explain "why does biomass start on a
    declining trend" beyond guessing."""
    m = models.describe_models("CMSY++")["model"]
    role = m["surimi_role"]
    assert "stb.low" in role and "endb.low" in role
    assert "1.21" in role
    assert "0.256" in role and "0.721" in role


def test_coupling_story_is_grounded_hub_and_spoke():
    for q in ["coupling", "how are the models connected", "how does EwE connect to POSEIDON"]:
        out = models.describe_models(q)
        assert out.get("topic") == "coupling", q
        c = out["coupling"]
        assert "Controller" in c and "EcologyService" in c and "ControllerService" in c
        assert "GetBiomass" in c and "UpdateBiomass" in c
        assert "never calls or resolves another model" in c


def test_assistant_self_knowledge_d37():
    """The assistant should know it IS the D3.7 NLP assessment work."""
    m = models.describe_models("NLP assessment")["model"]
    assert "D3.7" in m["surimi_role"]
    assert "NORCE" in m["developer"] or "NORCE" in m["surimi_role"]


def test_mice_runtime_caveat():
    m = models.describe_models("MICE")["model"]
    assert "D3.4" in m["surimi_role"]
    assert "not" in m["surimi_role"].lower() and "runtime" in m["surimi_role"].lower()


def test_every_entry_has_grounded_sources_and_surimi_role():
    for m in models.MODELS:
        assert m["sources"], f"{m['id']} missing sources"
        assert any("references:" in s for s in m["sources"]), f"{m['id']} not grounded in references"
        assert m["surimi_role"], f"{m['id']} missing surimi_role"
        assert m.get("kind") and m.get("layer"), f"{m['id']} missing kind/layer"


def test_miss_is_not_absence_claim():
    out = models.describe_models("Atlantis")
    assert out["found"] is False
    assert "It does not mean SURIMI does not use it" in out["note"]
    assert out["known_models"]


def test_value_chain_gets_sales_with_values_and_its_handler_is_a_stub():
    """Q-E3-5: 'how is the value chain informed by price changes'. Verified in
    the partner code on 2026-09-30: the Controller sends the value chain sales
    statistics with BOTH quantity and value (EUR) per species, fleet segment and
    market, so market prices reach it indirectly through the values; no price
    series is passed. Its handler UpdateSales() takes no argument and returns
    true, so the data is discarded. The earlier text said the value chain 'never
    receives price' and blamed two endpoints that are not on the path used."""
    m = models.describe_models("value chain")["model"]
    role = m["surimi_role"]
    low = role.lower()
    assert "quantity and value" in low
    assert "updatesales()" in low and "returns true" in low
    assert "never receives price" not in low and "does not receive price" not in low

    coupling = models.describe_models("coupling")["coupling"]
    cl = coupling.lower()
    assert "value chain" in cl and "quantity and value" in cl
    assert "never receives price" not in cl