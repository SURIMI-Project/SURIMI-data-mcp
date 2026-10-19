"""Q-E6-1 ("Where are these data taken from?") needs a citation reachable
from a served tool result, not just present in `datasets.yaml`. list_datasets
already has this exact lesson pinned for scope_note: only the fields in its
output tuple ever reach the model, so a bare dict key is inert. citation
gets the same treatment here.
"""
from surimi_data_mcp.datasets import catalog


def test_wmed_empirical_biomass_entry_carries_a_citation():
    d = catalog.find_dataset("surimi-biomass-wmed-empirical")
    assert d.get("citation")
    assert "SURIMI" in d["citation"]


def test_list_datasets_surfaces_the_citation_field():
    out = catalog.list_datasets(domain="surimi")
    entry = next(d for d in out["datasets"] if d["id"] == "surimi-biomass-wmed-empirical")
    assert entry.get("citation")
    assert entry["citation"] == catalog.find_dataset("surimi-biomass-wmed-empirical")["citation"]


def test_a_dataset_with_no_citation_omits_the_key_rather_than_serving_empty():
    out = catalog.list_datasets(domain="eu_dcf")
    entry = next(d for d in out["datasets"] if d["id"] == "eu-dcf-economic-aegean")
    assert "citation" not in entry
