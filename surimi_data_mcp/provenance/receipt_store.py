"""In-memory receipt store that keeps `/audit#sig:<signature>` links short.

`audit_link` stores a receipt here when its base64 fragment would be too long, and
`GET /audit/receipt/{signature}` returns it. Eviction or a restart drops entries, and the audit page
then asks the user to paste the receipt. The key is the receipt's HMAC signature.

An entry is the canonical serialization, `json.dumps(receipt, sort_keys=True, separators=(",", ":"),
ensure_ascii=False)` encoded as UTF-8, which is also what `audit_link` builds its fragment from.
`get()` returns the bytes unchanged.

The store holds at most `_MAX_BYTES` in total and `_MAX_ENTRIES` entries, and evicts the least
recently used entry first.
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from typing import Any, Optional

_MAX_ENTRIES = 4096
_MAX_BYTES = 64 * 1024 * 1024  # 64 MB total across all entries
_LOCK = threading.Lock()
_STORE: "OrderedDict[str, bytes]" = OrderedDict()
_TOTAL_BYTES = 0


def put(receipt: dict[str, Any]) -> Optional[str]:
    """Store a receipt under its signature and return the signature, or None when it is unsigned.

    A caller that already holds the canonical bytes should call `put_raw()` and skip the second serialization.
    """
    sig = receipt.get("signature") if isinstance(receipt, dict) else None
    if not sig or not isinstance(sig, str):
        return None
    raw = json.dumps(receipt, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    return put_raw(sig, raw)


def put_raw(sig: str, raw: bytes) -> Optional[str]:
    """Store canonical receipt bytes under `sig` and return `sig`.

    Returns None when `sig` is not alphanumeric, the bytes are empty, or they alone exceed the byte budget.
    """
    global _TOTAL_BYTES
    if not isinstance(sig, str) or not sig or not sig.isalnum():
        return None
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_BYTES:
        return None
    with _LOCK:
        old = _STORE.pop(sig, None)
        if old is not None:
            _TOTAL_BYTES -= len(old)
        _STORE[sig] = raw  # appended last = most recent
        _TOTAL_BYTES += len(raw)
        while _STORE and (len(_STORE) > _MAX_ENTRIES
                          or _TOTAL_BYTES > _MAX_BYTES):
            _, evicted = _STORE.popitem(last=False)
            _TOTAL_BYTES -= len(evicted)
    return sig


def get(signature: str) -> Optional[bytes]:
    """Return the canonical bytes stored for `signature` and mark them most recently used, or None."""
    with _LOCK:
        raw = _STORE.get(signature)
        if raw is not None:
            _STORE.move_to_end(signature)
        return raw


def reset() -> None:
    """Empty the store. Tests call it."""
    global _TOTAL_BYTES
    with _LOCK:
        _STORE.clear()
        _TOTAL_BYTES = 0
