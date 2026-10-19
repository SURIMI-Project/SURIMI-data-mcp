"""audit_url_for: shareable /audit#... links, in two forms.

Small receipts stay self-contained: the fragment is the canonical (sort_keys,
compact) receipt JSON, urlsafe base64 with padding stripped, which the /audit
page JS decodes. Receipts whose b64 fragment would exceed
audit_link._FRAGMENT_MAX chars are stored in receipt_store and linked as
`#sig:<signature>` (~constant length), the audit page fetches them back via
GET /audit/receipt/{signature}. Unsigned oversize receipts fall back to the
long self-contained form (they can't be fetched by signature; never silently
drop the link). The link is a SIBLING of the receipt, never a key inside it
(receipts are signed; adding a key would invalidate the signature).
"""
import base64
import json

import pytest

from surimi_data_mcp.provenance import audit_link
from surimi_data_mcp.provenance import receipt_store


@pytest.fixture(autouse=True)
def _clean_store():
    receipt_store.reset()
    yield
    receipt_store.reset()


def test_audit_url_roundtrip(monkeypatch):
    monkeypatch.setenv("SURIMI_PUBLIC_BASE_URL", "https://mcp.example")
    r = {"tool_id": "aggregate", "signature": "ab" * 32}
    url = audit_link.audit_url_for(r)
    assert url.startswith("https://mcp.example/audit#")
    frag = url.split("#", 1)[1]
    assert not frag.startswith("sig:")  # small receipt stays self-contained
    decoded = json.loads(base64.urlsafe_b64decode(frag + "=" * (-len(frag) % 4)))
    assert decoded == r


def test_audit_url_none_without_base(monkeypatch):
    monkeypatch.delenv("SURIMI_PUBLIC_BASE_URL", raising=False)
    assert audit_link.audit_url_for({"tool_id": "x"}) is None


def test_audit_url_none_for_non_dict(monkeypatch):
    monkeypatch.setenv("SURIMI_PUBLIC_BASE_URL", "https://mcp.example")
    assert audit_link.audit_url_for(None) is None
    assert audit_link.audit_url_for("not-a-receipt") is None


def test_audit_url_none_for_empty_receipt(monkeypatch):
    """{} must not mint a meaningless '#e30' link."""
    monkeypatch.setenv("SURIMI_PUBLIC_BASE_URL", "https://mcp.example")
    assert audit_link.audit_url_for({}) is None


def test_audit_url_trailing_slash_base_normalized(monkeypatch):
    monkeypatch.setenv("SURIMI_PUBLIC_BASE_URL", "https://mcp.example/")
    url = audit_link.audit_url_for({"tool_id": "x"})
    assert url.startswith("https://mcp.example/audit#")
    assert "//audit" not in url.replace("https://", "")


def test_big_receipt_gets_short_sig_link(monkeypatch):
    """A receipt whose b64 fragment exceeds _FRAGMENT_MAX is stored and linked
    as #sig:<signature>, ~constant URL length, exact receipt fetchable back."""
    monkeypatch.setenv("SURIMI_PUBLIC_BASE_URL", "https://mcp.example")
    sig = "cd" * 32
    big = {"tool_id": "query_data", "signature": sig,
           "output_value": {"rows": ["x" * 4000]}}
    raw = json.dumps(big, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    frag_len = len(base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="))
    assert frag_len > audit_link._FRAGMENT_MAX  # premise of the test

    url = audit_link.audit_url_for(big)
    assert url == f"https://mcp.example/audit#sig:{sig}"
    assert len(url) < 200  # short, ~constant, never the multi-KB form
    # store keeps the CANONICAL bytes, the exact serialization the b64
    # fragment is built from; the audit route serves them verbatim as application/json
    assert receipt_store.get(sig) == raw
    assert json.loads(receipt_store.get(sig)) == big


def test_big_unsigned_receipt_falls_back_to_long_form(monkeypatch):
    """An unsigned oversize receipt can't be fetched by signature, fall back
    to the long self-contained fragment rather than dropping the link."""
    monkeypatch.setenv("SURIMI_PUBLIC_BASE_URL", "https://mcp.example")
    big = {"tool_id": "query_data", "output_value": {"rows": ["x" * 4000]}}
    url = audit_link.audit_url_for(big)
    assert url is not None
    frag = url.split("#", 1)[1]
    assert not frag.startswith("sig:")
    decoded = json.loads(base64.urlsafe_b64decode(frag + "=" * (-len(frag) % 4)))
    assert decoded == big


def test_small_receipt_not_stored(monkeypatch):
    """Self-contained links don't pollute the bounded store."""
    monkeypatch.setenv("SURIMI_PUBLIC_BASE_URL", "https://mcp.example")
    sig = "ef" * 32
    audit_link.audit_url_for({"tool_id": "aggregate", "signature": sig})
    assert receipt_store.get(sig) is None
