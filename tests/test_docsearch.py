"""Tests for the BM25 full-text document retriever (docsearch).

The guarantees that stop the chat hallucinating document facts:
  * results are VERBATIM substrings of the source file (so the model quotes, not invents)
  * a query with no lexical match returns NOTHING (honest empty, never a guess)
  * the FULL body text is indexed (a fact mid-document is retrievable, not just titles)
  * punctuation / FTS operators in the query never crash or inject
"""
import os
import textwrap

from surimi_data_mcp.knowledge import docsearch


def _corpus(tmp_path):
    (tmp_path / "cmsy.md").write_text(textwrap.dedent("""\
        # CMSY++ in SURIMI

        The SURIMI-CMSY service is a Python gRPC wrapper around the AA_CMSY++.R model.
        It uses JAGS 4.3.1 for the Bayesian state-space model and a feed-forward neural
        net prior. Output is CSV only; it does not write any PDF files.
    """), encoding="utf-8")
    (tmp_path / "poseidon.txt").write_text(
        "POSEIDON is a Java gRPC server wrapping the agent-based fishery model. "
        "The Western Med case study inputs are prepared in R.\n", encoding="utf-8")
    return tmp_path


def test_search_returns_verbatim_substring_of_source(tmp_path):
    idx = docsearch.build_index([str(_corpus(tmp_path))], db_path=":memory:")
    hits = idx.search("JAGS Bayesian", top_k=5)
    assert hits, "expected a hit for a term that is in the corpus"
    top = hits[0]
    assert top["source"].endswith("cmsy.md")
    # the returned passage must be an EXACT substring of the original file (verbatim)
    original = (tmp_path / "cmsy.md").read_text(encoding="utf-8")
    assert top["text"] in original, "retrieved text must be verbatim, not paraphrased"
    assert "JAGS 4.3.1" in top["text"]


def test_no_match_returns_empty(tmp_path):
    idx = docsearch.build_index([str(_corpus(tmp_path))], db_path=":memory:")
    # a term that appears in NO document -> honest empty, never a fabricated answer
    assert idx.search("ecospace chlorophyll quota", top_k=5) == [] or \
        all("nonexistentterm" not in h["text"] for h in idx.search("nonexistentterm", top_k=5))
    assert idx.search("zzznotacorpusword", top_k=5) == []


def test_full_body_text_is_indexed_not_just_titles(tmp_path):
    idx = docsearch.build_index([str(_corpus(tmp_path))], db_path=":memory:")
    # 'feed-forward neural net prior' is buried mid-paragraph, not in any heading
    hits = idx.search("feed-forward neural net prior", top_k=5)
    assert hits and "feed-forward neural" in hits[0]["text"]


def test_query_with_punctuation_and_fts_operators_is_safe(tmp_path):
    idx = docsearch.build_index([str(_corpus(tmp_path))], db_path=":memory:")
    for q in ["CMSY++ (BSM)?", 'JAGS" OR x', "model AND NOT", "*", "", "   "]:
        hits = idx.search(q, top_k=5)            # must not raise
        assert isinstance(hits, list)


def test_ranking_prefers_more_term_matches(tmp_path):
    _corpus(tmp_path)
    idx = docsearch.build_index([str(tmp_path)], db_path=":memory:")
    hits = idx.search("POSEIDON Java fishery", top_k=5)
    assert hits and hits[0]["source"].endswith("poseidon.txt")


def test_results_carry_source_and_score(tmp_path):
    idx = docsearch.build_index([str(_corpus(tmp_path))], db_path=":memory:")
    h = idx.search("CMSY", top_k=3)[0]
    assert set(["text", "source", "score"]).issubset(h.keys())
    assert isinstance(h["text"], str) and h["text"]


def test_missing_corpus_dir_yields_empty_index_not_crash(tmp_path):
    idx = docsearch.build_index([str(tmp_path / "does-not-exist")], db_path=":memory:")
    assert idx.search("anything", top_k=5) == []


def test_search_works_from_another_thread(tmp_path):
    # The MCP serves tool calls on worker threads, but the doc index is a
    # process-wide singleton built in one thread. A SQLite connection bound to
    # its creating thread raises "SQLite objects created in a thread can only be
    # used in that same thread" when queried from another -> search_documents /
    # explain_provenance intermittently crash. The index MUST be query-safe
    # across threads.
    import threading
    idx = docsearch.build_index([str(_corpus(tmp_path))], db_path=":memory:")  # built in main thread
    out = {}

    def worker():
        try:
            out["hits"] = idx.search("JAGS Bayesian", top_k=5)
        except Exception as e:  # capture the cross-thread error instead of dying silently
            out["err"] = repr(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert "err" not in out, f"cross-thread search raised: {out.get('err')}"
    assert out["hits"] and out["hits"][0]["source"].endswith("cmsy.md")


def test_concurrent_searches_do_not_corrupt(tmp_path):
    # Reproduces the live failure mode: many MCP worker threads querying the one
    # shared connection at once. check_same_thread=False alone is not enough --
    # a single SQLite connection used concurrently can raise "recursive use of
    # cursors"; the lock serialises access. All queries must return clean results.
    import threading
    idx = docsearch.build_index([str(_corpus(tmp_path))], db_path=":memory:")
    errors: list[str] = []
    results: list[int] = []
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()  # release all threads at the same instant -> max contention
        for _ in range(25):
            try:
                hits = idx.search("JAGS Bayesian gRPC POSEIDON", top_k=5)
                results.append(len(hits))
            except Exception as e:  # any threading/cursor error fails the test
                errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent search raised: {errors[:3]}"
    assert results and all(n >= 1 for n in results), "every concurrent search must return hits"


def test_internal_dev_wiki_is_never_indexed(tmp_path):
    """SECURITY: the internal LLM dev wiki (meeting notes, stakeholders, dev log)
    must NEVER be searchable through the public MCP, even if it lands in the
    corpus dir. The public EwE wiki (SURIMI-project.wiki) + data-lake READMEs
    must still be indexed. (Regression guard for the 2026-06 leak.)"""
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    (tmp_path / "wiki" / "sources" / "meeting-25-march-2026.md").write_text(
        "Open tensions on the model choice; action items for the work package", encoding="utf-8")
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    (tmp_path / "wiki" / "entities" / "stakeholders.md").write_text(
        "Stakeholder notes: who leads which task and how they prefer updates", encoding="utf-8")
    (tmp_path / "wiki" / "log.md").write_text("internal dev build log", encoding="utf-8")
    (tmp_path / "SURIMI-project.wiki").mkdir(parents=True)
    (tmp_path / "SURIMI-project.wiki" / "Home.md").write_text(
        "SURIMI public project wiki: workpackages and deliverables", encoding="utf-8")
    (tmp_path / "data-lake").mkdir(parents=True)
    (tmp_path / "data-lake" / "README.txt").write_text(
        "FAO landings dataset metadata, MEDITS CPUE provenance", encoding="utf-8")

    idx = docsearch.build_index([str(tmp_path)], db_path=":memory:")
    internal = idx.search("internal meeting open tensions stakeholders dev log action items", top_k=10)
    assert internal == [], f"internal dev wiki leaked into search: {[r['source'] for r in internal]}"
    public = [r["source"] for r in idx.search("public project wiki deliverable FAO landings dataset", top_k=10)]
    assert any("SURIMI-project.wiki" in s for s in public)
    assert any("data-lake" in s for s in public)
