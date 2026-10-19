"""GET /sources (manifest index) and GET /sources/{file} (download of a listed file).

Only the `file` values listed in `sources_manifest.json` are served: no path traversal and no other
pod files. `SURIMI_DATA_DIR` is tried first (development). On the deployed pod the file is streamed
from S3 or MinIO at `SURIMI_SOURCES_S3` (for example `s3://project-surimi/mcp_data/tables`) with the
pod's existing credentials.
"""
from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import Iterator, Optional

import anyio
from starlette.requests import Request
from starlette.responses import (FileResponse, JSONResponse, Response,
                                 StreamingResponse)
from starlette.routing import Route

from surimi_data_mcp.provenance import lineage

logger = logging.getLogger(__name__)


def _whitelisted(filename: str) -> bool:
    """Return True when `filename` equals a manifest `file` value and has no `..` or leading `/`."""
    files = {e.get("file") for e in lineage.all_sources().values()
             if isinstance(e, dict)}
    return filename in files and ".." not in filename and not filename.startswith("/")


async def _index(request: Request) -> JSONResponse:
    return JSONResponse(lineage.all_sources())


def _local_path(filename: str) -> Optional[Path]:
    base = os.environ.get("SURIMI_DATA_DIR", "")
    if not base:
        return None
    root = Path(base).resolve()
    p = (root / filename).resolve()
    if root not in p.parents and p != root:  # defense-in-depth: stay under root
        return None
    return p if p.is_file() else None


def _s3_endpoint_url() -> Optional[str]:
    """Return the S3 endpoint from `AWS_ENDPOINT_URL`, `S3_ENDPOINT_URL` or `AWS_S3_ENDPOINT_URL`, in that order, or None.

    Without an endpoint, boto3 talks to AWS S3.
    """
    for key in ("AWS_ENDPOINT_URL", "S3_ENDPOINT_URL", "AWS_S3_ENDPOINT_URL"):
        val = os.environ.get(key)
        if val:
            return val
    return None


def _s3_stream(filename: str) -> Optional[Iterator[bytes]]:
    """Return an iterator over the file's bytes from `SURIMI_SOURCES_S3`, or None when it is unset or the fetch fails."""
    uri = os.environ.get("SURIMI_SOURCES_S3", "")
    if not uri.startswith("s3://"):
        return None
    import boto3  # deferred: only needed on deployed pods with S3 fallback
    bucket, _, prefix = uri[5:].partition("/")
    client = boto3.client("s3", endpoint_url=_s3_endpoint_url())
    key = f"{prefix.rstrip('/')}/{filename}" if prefix else filename
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        logger.warning("S3 source fetch failed for %s/%s: %s", bucket, key, exc)
        return None
    return obj["Body"].iter_chunks(chunk_size=1 << 20)


async def _download(request: Request) -> Response:
    filename = request.path_params["file"]
    if not _whitelisted(filename):
        return Response("not a known source file", status_code=404)
    basename = filename.rsplit("/", 1)[-1]
    media_type = mimetypes.guess_type(basename)[0] or "application/octet-stream"
    headers = {"content-disposition": f'attachment; filename="{basename}"'}
    local = _local_path(filename)
    if local is not None:
        return FileResponse(local, media_type=media_type, filename=basename)
    # boto3's `get_object` is synchronous, so it runs in a worker thread and a slow MinIO
    # does not block the event loop.
    body = await anyio.to_thread.run_sync(_s3_stream, filename)
    if body is not None:
        return StreamingResponse(body, media_type=media_type, headers=headers)
    return Response("source file unavailable", status_code=404)


def make_sources_routes() -> list[Route]:
    """Return the `/sources` index route and the download route."""
    return [Route("/sources", _index, methods=["GET"]),
            Route("/sources/{file:path}", _download, methods=["GET"])]
