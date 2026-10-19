"""Q-E11-3: 'how much of this is observed vs. modelled/estimated' has no single
answer today. ANSWER-CONTRACT.md specifies an `origin` field, never omitted,
for exactly this. Smallest fix: reuse the already-known observed/modelled
split (`kind`) as `origin`, surface it in list_datasets, and give query_metric
its own `origin` per metric -- enforced, not best-effort.
"""
from __future__ import annotations

from surimi_data_mcp.datasets import catalog


def test_every_catalog_entry_carries_an_origin():
    missing = [d["id"] for d in catalog.DATASETS if not d.get("origin")]
    assert not missing, f"datasets with no origin: {missing}"


def test_origin_matches_the_known_observed_modelled_split():
    for d in catalog.DATASETS:
        assert d["origin"] == d.get("kind"), d["id"]


def test_list_datasets_surfaces_origin():
    out = catalog.list_datasets(domain="eu_dcf")
    entry = next(d for d in out["datasets"] if d["id"] == "aer-economic-2013-2023")
    assert entry["origin"] == "observed"


def test_list_datasets_surfaces_origin_for_a_modelled_dataset():
    out = catalog.list_datasets(domain="surimi")
    entry = next(d for d in out["datasets"] if d["id"] == "surimi-sales-aegean-simulated")
    assert entry["origin"] == "modelled"
