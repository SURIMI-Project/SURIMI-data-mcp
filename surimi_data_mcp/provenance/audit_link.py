"""Builds the shareable `/audit#...` link that the server returns next to each receipt.

The link is kept outside the receipt dict. The receipt is already signed, and adding a key to it
would break its signature when it is checked again.

The `/audit` page reads one of two fragment forms. For a small receipt, the fragment is the
canonical receipt JSON (sorted keys, compact separators) encoded as urlsafe base64 with the `=`
padding removed, which the page's JavaScript pads and decodes. When that fragment would be longer
than `_FRAGMENT_MAX` characters, the link is `#sig:<signature>` and the page fetches the receipt
from `GET /audit/receipt/{signature}`, which reads `receipt_store`. If the store has lost the entry
(eviction or a restart), the page asks the user to paste the receipt. An unsigned receipt cannot be
fetched by signature, so it keeps the long form.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Optional

from surimi_data_mcp.provenance import receipt_store

# Longest base64 fragment kept in the link. A longer receipt is stored and linked by signature.
_FRAGMENT_MAX = 3072


def audit_url_for(receipt: dict) -> Optional[str]:
    """Return the `/audit` link for a receipt, or None when `SURIMI_PUBLIC_BASE_URL` is unset or the receipt is empty."""
    base = os.environ.get("SURIMI_PUBLIC_BASE_URL", "").rstrip("/")
    if not base or not isinstance(receipt, dict) or not receipt:
        return None
    raw = json.dumps(receipt, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    frag = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if len(frag) <= _FRAGMENT_MAX:
        return f"{base}/audit#{frag}"
    # Reuse the canonical bytes already serialized. `put_raw` refuses a missing or
    # implausible signature, and the link then falls back to the long form.
    sig = receipt.get("signature")
    if isinstance(sig, str) and receipt_store.put_raw(sig, raw):
        return f"{base}/audit#sig:{sig}"
    return f"{base}/audit#{frag}"
