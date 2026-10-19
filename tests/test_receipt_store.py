"""receipt_store: byte-budgeted in-memory LRU keyed by signature.

Backs the short /audit#sig:<signature> links: receipts too big for a URL
fragment are stored here at issue time and fetched back by the audit page
(GET /audit/receipt/{signature}). Eviction or a restart loses entries, which
sends the reader to the audit page's paste flow and never breaks verification.

Bytes contract (the audit route consumes this): entries are the CANONICAL receipt
serialization, json.dumps(receipt, sort_keys=True, separators=(",", ":"),
ensure_ascii=False).encode("utf-8"), and get() returns those bytes verbatim,
ready to be served as application/json. The store is bounded by a total byte
budget (_MAX_BYTES) with an entry-count cap (_MAX_ENTRIES) as secondary bound,
so unauthenticated clients on a public pod can't balloon memory.
"""
from __future__ import annotations

import json

import pytest

from surimi_data_mcp.provenance import receipt_store


@pytest.fixture(autouse=True)
def _clean_store():
    receipt_store.reset()
    yield
    receipt_store.reset()


def _receipt(sig: str, **extra) -> dict:
    return {"tool_id": "aggregate", "signature": sig, **extra}


def _canonical(receipt: dict) -> bytes:
    return json.dumps(receipt, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def test_put_get_roundtrip_returns_canonical_bytes():
    r = _receipt("ab" * 32, value=42)
    sig = receipt_store.put(r)
    assert sig == "ab" * 32
    raw = receipt_store.get(sig)
    assert isinstance(raw, bytes)
    assert raw == _canonical(r)
    assert json.loads(raw) == r  # verbatim-serveable as application/json


def test_unsigned_receipt_not_stored():
    assert receipt_store.put({"tool_id": "aggregate"}) is None
    # non-string signature is also rejected
    assert receipt_store.put({"signature": 123}) is None
    assert receipt_store.get("123") is None


def test_put_raw_roundtrip():
    sig = "cd" * 32
    raw = _canonical(_receipt(sig))
    assert receipt_store.put_raw(sig, raw) == sig
    assert receipt_store.get(sig) == raw


def test_put_raw_rejects_bad_signatures():
    raw = b'{"x":1}'
    assert receipt_store.put_raw("", raw) is None
    assert receipt_store.put_raw("sig:../evil", raw) is None  # non-alnum
    assert receipt_store.put_raw("ab cd", raw) is None
    assert receipt_store.put_raw(None, raw) is None  # type: ignore[arg-type]
    assert receipt_store.put_raw("abcd", b"") is None  # empty payload
    assert receipt_store.get("abcd") is None


def test_lru_eviction_at_entry_cap(monkeypatch):
    monkeypatch.setattr(receipt_store, "_MAX_ENTRIES", 3)
    for i in range(4):
        receipt_store.put(_receipt(f"sig{i}"))
    # cap is 3: the oldest (sig0) was evicted, the rest survive
    assert receipt_store.get("sig0") is None
    assert receipt_store.get("sig1") is not None
    assert receipt_store.get("sig2") is not None
    assert receipt_store.get("sig3") is not None


def test_recently_got_survives_eviction_over_colder_entry(monkeypatch):
    monkeypatch.setattr(receipt_store, "_MAX_ENTRIES", 3)
    for i in range(3):
        receipt_store.put(_receipt(f"sig{i}"))
    # touch sig0, now sig1 is the coldest
    assert receipt_store.get("sig0") is not None
    receipt_store.put(_receipt("sig3"))
    assert receipt_store.get("sig1") is None      # coldest evicted
    assert receipt_store.get("sig0") is not None  # recently-got survived


def test_byte_budget_evicts_lru_until_under(monkeypatch):
    """Total stored bytes stay <= _MAX_BYTES: oldest entries are evicted."""
    monkeypatch.setattr(receipt_store, "_MAX_BYTES", 250)
    for i in range(4):  # each canonical entry is ~90 bytes
        receipt_store.put(_receipt(f"sig{i}", pad="x" * 40))
    assert receipt_store.get("sig0") is None  # evicted to fit the budget
    assert receipt_store.get("sig3") is not None
    total = sum(len(receipt_store.get(f"sig{i}") or b"") for i in range(4))
    assert 0 < total <= 250


def test_single_entry_over_budget_not_stored(monkeypatch):
    """One oversize payload must not wipe the store then fail to fit."""
    monkeypatch.setattr(receipt_store, "_MAX_BYTES", 100)
    assert receipt_store.put_raw("aa" * 16, b"x" * 101) is None
    assert receipt_store.get("aa" * 16) is None
    # store stays usable
    assert receipt_store.put_raw("bb" * 16, b"x" * 50) == "bb" * 16


def test_reput_same_sig_refreshes_recency_and_does_not_double_count(monkeypatch):
    monkeypatch.setattr(receipt_store, "_MAX_ENTRIES", 3)
    for i in range(3):
        receipt_store.put(_receipt(f"sig{i}"))
    # re-put sig0, now sig1 is the coldest
    receipt_store.put(_receipt("sig0"))
    receipt_store.put(_receipt("sig3"))
    assert receipt_store.get("sig1") is None      # coldest evicted
    assert receipt_store.get("sig0") is not None  # re-put refreshed recency
    # byte total reflects single copies, not accumulated re-puts
    expected = sum(len(receipt_store.get(s)) for s in ("sig0", "sig2", "sig3"))
    assert receipt_store._TOTAL_BYTES == expected


def test_reset_clears():
    receipt_store.put(_receipt("ab" * 32))
    receipt_store.reset()
    assert receipt_store.get("ab" * 32) is None
    assert receipt_store._TOTAL_BYTES == 0
