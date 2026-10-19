"""The audit page and its endpoints: `GET /audit`, `POST /audit/verify`, `GET /audit/receipt/{signature}` and `GET /audit/dataset/{dataset_id}`.

The receipt travels in the URL fragment (`#<base64url>` or `#sig:<signature>`), so it never reaches the
server logs, and the page posts it to `/audit/verify`. `verify_and_recompute()` checks the signature
first, so a tampered receipt is reported and never recomputed.

`/audit/verify` recomputes `aggregate` and `query_data` receipts, as the MCP `verify` tool does
(`tools/verify.py`). `app_factory` passes in a `query_client_factory` built from its own copy of the
database client in `tools/common.py`, with the cube fallback, so that mounting these routes does not
import the tools package. `model_*` receipts are checked by signature only, because no server
recomputes them.

Accepted risk: any holder of a validly signed receipt can post to `/audit/verify` with `recompute=true`
and start a real DuckDB query. The recompute runs in a worker thread (`anyio.to_thread`), so the event
loop stays free, and `_RECOMPUTE_LIMITER` lets one recompute run at a time. DuckDB's one shared
connection is not safe for concurrent cursors, and the single slot also slows a flood of replays.
"""
from __future__ import annotations

import base64
import binascii
import functools
import json
from typing import Any, Callable, Optional

import anyio
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from surimi_data_mcp.provenance import receipt_store
from surimi_data_mcp.provenance import explain
from surimi_data_mcp.provenance.verification import verify_and_recompute
from surimi_data_mcp.paths import APP_ROOT

_PAGE = APP_ROOT / "static" / "audit.html"

# One token: anonymous recomputes run one at a time, because the single shared DuckDB
# connection is not safe for concurrent cursors.
_RECOMPUTE_LIMITER = anyio.CapacityLimiter(1)

# Bodies larger than this are rejected before parsing.
_MAX_BODY_BYTES = 1_048_576


def _b64url_decode(frag: str) -> bytes:
    """Decode a urlsafe base64 fragment, restoring the `=` padding that `audit_link` strips.

    Raises on a malformed alphabet or length.
    """
    padded = frag + "=" * (-len(frag) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


class _ResolveError(Exception):
    """Raised when the request body cannot be turned into a receipt dict.

    It carries the `JSONResponse` that the route should return, with a message and the status code.
    """

    def __init__(self, response: JSONResponse) -> None:
        super().__init__()
        self.response = response


def _resolve_receipt(body: dict) -> dict:
    """Return the receipt dict for a request body, decoded from the signed bytes the page holds.

    Python's `json` keeps a float such as `1320.0` as written, so the signature still matches the bytes.
    A JavaScript re-serialization would drop the `.0`. The body keys are tried in this order:

      * `receipt_b64` (str): the URL-fragment form.
      * `receipt_sig` (str): the stored bytes for that signature.
      * `receipt_text` (str): the pasted JSON text.
      * `receipt` (dict): an already parsed receipt, for server-side callers.

    Raises `_ResolveError` on a decode, parse or lookup failure, or when the result is not a JSON object.
    """
    if isinstance(body.get("receipt_b64"), str):
        frag = body["receipt_b64"]
        try:
            raw = _b64url_decode(frag)
        except (binascii.Error, ValueError):
            raise _ResolveError(JSONResponse(
                {"error": "receipt_b64 is not valid base64url"}, status_code=400))
        return _parse_receipt_bytes(raw, source="receipt_b64")

    if isinstance(body.get("receipt_sig"), str):
        sig = body["receipt_sig"]
        raw = receipt_store.get(sig)
        if raw is None:
            raise _ResolveError(JSONResponse(
                {"error": "receipt not found",
                 "hint": "the short link expired (server restart or cache "
                         "eviction) - paste the receipt JSON into the page "
                         "instead"},
                status_code=404))
        return _parse_receipt_bytes(raw, source="receipt_sig")

    if isinstance(body.get("receipt_text"), str):
        return _parse_receipt_bytes(
            body["receipt_text"].encode("utf-8"), source="receipt_text")

    receipt = body.get("receipt")
    if isinstance(receipt, dict):
        return receipt

    raise _ResolveError(JSONResponse(
        {"error": "provide one of receipt_b64, receipt_sig, receipt_text, or "
                  "receipt (object)"},
        status_code=400))


def _parse_receipt_bytes(raw: bytes, *, source: str) -> dict:
    """Parse the signed bytes into a receipt dict. Raises `_ResolveError` when they are not a JSON object.

    `json.loads('1320.0')` gives `1320.0` and `json.dumps` writes `1320.0` again, so the signature
    still covers exactly these bytes.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        # RecursionError: a deeply-nested JSON bomb (well under the body cap)
        # must still be a clean 400 on this public endpoint, never a 500.
        raise _ResolveError(JSONResponse(
            {"error": f"{source} did not contain valid JSON"}, status_code=400))
    if not isinstance(obj, dict):
        raise _ResolveError(JSONResponse(
            {"error": f"{source} must decode to a receipt object"},
            status_code=400))
    return obj


def make_audit_routes(
    *,
    cube_factory: Callable[[], Any],
    query_client_factory: Optional[Callable[[], Any]] = None,
) -> list[Route]:
    """Return the four `/audit` routes.

    `cube_factory` opens the cube for the `aggregate` recompute, and `query_client_factory` gives the
    client for the `query_data` recompute.
    """

    async def page(request):
        return HTMLResponse(_PAGE.read_text(encoding="utf-8"))

    async def verify(request):
        # Body cap: uses the declared Content-Length when there is one.
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                return JSONResponse({"error": "invalid Content-Length"},
                                    status_code=400)
            if declared > _MAX_BODY_BYTES:
                return JSONResponse(
                    {"error": "body too large (max 1MB) - a receipt is a "
                              "few KB"},
                    status_code=413)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an object"},
                                status_code=400)
        # Resolve the receipt from the signed bytes the page holds (base64 fragment, stored
        # bytes or pasted text). A JavaScript re-serialization would drop the `.0` of a float
        # SUM and break the signature of a valid receipt.
        try:
            receipt = _resolve_receipt(body)
        except _ResolveError as err:
            return err.response
        # `verify_and_recompute` is synchronous and, on recompute, runs a DuckDB query,
        # so it runs in a worker thread.
        out = await anyio.to_thread.run_sync(functools.partial(
            verify_and_recompute,
            receipt,
            recompute=bool(body.get("recompute")),
            cube_factory=cube_factory,
            query_client_factory=query_client_factory,
        ), limiter=_RECOMPUTE_LIMITER)
        return JSONResponse(out)

    async def fetch_receipt(request):
        sig = request.path_params["signature"]
        raw = receipt_store.get(sig)
        if raw is None:
            return JSONResponse(
                {"error": "receipt not found",
                 "hint": "the short link expired (server restart or cache "
                         "eviction) - paste the receipt JSON into the page "
                         "instead"},
                status_code=404)
        # The canonical bytes unchanged, so the signature still covers exactly these bytes.
        return Response(raw, media_type="application/json")

    async def dataset(request):
        # The catalogue's notes on one dataset, for the page's "About this table" card. The
        # receipt carries only the dataset ids.
        notes = explain.dataset_notes(request.path_params["dataset_id"])
        if notes is None:
            return JSONResponse({"error": "no such dataset in the catalogue"}, status_code=404)
        return JSONResponse(notes)

    return [Route("/audit", page, methods=["GET"]),
            Route("/audit/verify", verify, methods=["POST"]),
            Route("/audit/receipt/{signature}", fetch_receipt, methods=["GET"]),
            Route("/audit/dataset/{dataset_id}", dataset, methods=["GET"])]
