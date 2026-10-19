"""Tools that explain: indicators, species codes, functional groups, the SURIMI documents, the models and
where a dataset came from.
"""
from typing import Optional

from surimi_data_mcp.engine import skills
from surimi_data_mcp.knowledge import docsearch, lookups, models
from surimi_data_mcp.mcp_server import log, mcp
from surimi_data_mcp.tools.common import attach_audit_urls

# ── Search tools ──

@mcp.tool()
def explain_indicator(
    indicator: str,
    dataset_id: Optional[str] = None,
) -> dict:
    """Explain what a data indicator or column means: its definition, unit and source."""
    log(f"[search] explain_indicator indicator={indicator}")
    return lookups.explain_indicator(indicator, dataset_id=dataset_id)


@mcp.tool()
def explain_species_code(code: str) -> dict:
    """Resolve a species code (PIL, HKE, ALB...) to its scientific name.

    Codes are not unique keys: four codes appear twice, split by life stage, so
    every matching entry is returned instead of the first one. Gear codes such
    as OTB are reported as not found, because no project file defines them.
    """
    log(f"[search] explain_species_code code={code}")
    return lookups.explain_species_code(code)


@mcp.tool()
def data_list_functional_groups(group: Optional[str] = None) -> dict:
    """List Ecopath functional groups, or the species in one named group.

    With no `group`, lists every functional-group name the EwE NWMed model
    declares (59). With `group` (matched case-insensitively as a substring,
    e.g. "small demersal"), returns the species that belong to it.

    For the group's BIOMASS, read `model_series` when present: small demersals
    are served on the sibling MODEL server as EcoOcean bd30cm (g m-2, monthly,
    1961-2009), via model_time_series / model_trend with region
    'Mediterranean'. The member species can also be queried by name in MEDITS
    survey data, which is a catch-rate index, not biomass (`survey_note`).
    """
    log(f"[search] data_list_functional_groups group={group}")
    return lookups.list_functional_groups(group)


@mcp.tool()
def search_documents(
    query: str,
    top_k: int = 5,
) -> dict:
    """Full-text search over the SURIMI documents (public project wiki, gRPC
    protocol, model repos, data-lake documents). BM25 lexical retrieval that
    returns VERBATIM passages with their source file.

    Ground every claim in the returned `text`: quote it and cite `source`. If the
    results are empty or do not contain the answer, say the documents do not cover
    it -- do NOT invent. Use for policy / methodology / model background the data
    tables do not hold. What a dataset was used for (which model or analysis it fed)
    is not in these documents: list_datasets reports it as `documented_use`.
    """
    log(f"[search] search_documents query={query[:60]}...")
    results = docsearch.search(query, top_k=top_k)
    return {
        "query": query,
        "results": results,
        "count": len(results),
        "backend": "bm25-fts5",
        "grounding": ("Each result's `text` is a verbatim passage from `source`. "
                      "Answer only from these passages and cite the source; if none "
                      "are relevant, say the documents do not cover it. What a dataset "
                      "was used for is reported by list_datasets as `documented_use`."),
    }


# ── Reference tools ──

@mcp.tool()
def describe_models(name: Optional[str] = None) -> dict:
    """Describe the fisheries / ecosystem modelling frameworks in the SURIMI ensemble.

    Call this for ANY question about a model or method by name -- Ecopath / Ecosim /
    EwE, CMSY / CMSY++, POSEIDON, MICE, etc. With no `name`, lists the known models
    and how SURIMI's layers fit together; with a `name` (e.g. 'EwE', 'Ecopath',
    'CMSY++', 'POSEIDON'), returns a grounded description with its SURIMI role and
    sources. Entries come from the SURIMI references or cited literature, and an
    entry marked general background comes from outside the SURIMI references. A
    miss here means the model is not in this knowledge base, which does not mean
    that SURIMI does not use it. Never fabricate provenance, versions or coupling.
    """
    log(f"[reference] describe_models name={name}")
    return models.describe_models(name)


@mcp.tool()
def explain_provenance(dataset_id: str, question: Optional[str] = None) -> dict:
    """Where a dataset comes from and how it was made, answered from the SURIMI documents and the build manifest.

    Runs the document search on the server and returns verbatim passages, their sources and the
    dataset's source-file entry from the build manifest.
    Use for ANY 'where is this from / what is the source / how was it collected'
    question. If no docs match, it says so; never fill the gap with general knowledge.
    """
    log(f"[skill] explain_provenance dataset={dataset_id} q={question}")
    import os
    secret = os.environ.get("SURIMI_RECEIPT_SECRET", "").encode("utf-8") or None
    try:
        return attach_audit_urls(skills.run_explain_provenance(
            docsearch.get_index(), dataset_id=dataset_id, question=question, secret=secret))
    except Exception as e:
        return {"error": str(e), "skill": "explain_provenance", "receipts": []}
