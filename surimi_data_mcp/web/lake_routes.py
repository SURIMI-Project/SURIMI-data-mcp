"""GET /sources/lake (index) and GET /sources/lake/{path} (download): a read-only copy of the data lake.

The raw SURIMI data lake is stored under sanitized lower_snake_case keys, with a `lake_manifest.json`
that maps each original lake path to its key, size and sha256. This module serves exactly the manifest
entries, addressed by the original path, so a script written against `references/SURIMI-DATA-LAKE/<path>`
can fetch the same file by name.

Only the manifest's `files` are served: no traversal, no listing of the bucket, no pod files. The
server only reads. The routes live under /sources because the EDITO ingress already forwards that
prefix, whereas a new top-level path would need a chart change and a relaunch.

Access is single sign-on only (lake_auth.py). A valid Keycloak token is required, and with no issuer
configured the route refuses instead of serving openly.

Where the files come from, in order:
  SURIMI_LAKE_DIR  a local directory holding lake_manifest.json and the keys (dev and tests)
  SURIMI_LAKE_CACHE  the copy on the server's volume, when the file is complete (lake_cache.py)
  SURIMI_LAKE_S3   s3://bucket/prefix, read with the pod's existing credentials

An entry may carry its own `prefix`, used instead of the base one. That serves a file whose stored copy
differs from the lake's from a second prefix without touching the first.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Optional

import anyio
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from surimi_data_mcp.web import lake_auth
from surimi_data_mcp.web import lake_cache

logger = logging.getLogger(__name__)

_TTL = 600.0
_cache: dict = {"at": 0.0, "files": None}
_RANGE = re.compile(r"^bytes=(\d+)-(\d*)$")


def reset() -> None:
    """Drop the cached manifest so the next request reads it again."""
    _cache.update(at=0.0, files=None)


def _s3_endpoint_url() -> Optional[str]:
    for name in ("AWS_ENDPOINT_URL", "S3_ENDPOINT_URL", "AWS_S3_ENDPOINT_URL"):
        if os.environ.get(name):
            return os.environ[name]
    return None


def _s3_client():
    import boto3  # deferred: only deployed pods read from S3
    return boto3.client("s3", endpoint_url=_s3_endpoint_url())


def _s3_parts() -> Optional[tuple[str, str]]:
    """Return `(bucket, prefix)` from `SURIMI_LAKE_S3`, or None when it is not an `s3://` URI."""
    uri = os.environ.get("SURIMI_LAKE_S3", "")
    if not uri.startswith("s3://"):
        return None
    bucket, _, prefix = uri[5:].partition("/")
    return bucket, prefix.strip("/")


def _read_manifest() -> Optional[list[dict]]:
    """Return the manifest's `files` list, or None when it cannot be read.

    It reads `SURIMI_LAKE_DIR` when set, otherwise S3, where it also saves a copy through `lake_cache`
    and falls back to that copy when S3 fails.
    """
    local = os.environ.get("SURIMI_LAKE_DIR", "")
    try:
        if local:
            raw = (Path(local) / "lake_manifest.json").read_bytes()
        elif (parts := _s3_parts()):
            bucket, prefix = parts
            key = f"{prefix}/lake_manifest.json" if prefix else "lake_manifest.json"
            try:
                raw = _s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
                lake_cache.save_manifest(raw)
            except Exception as exc:
                # S3 failed, for example because the daily token expired: use the copy on the volume.
                raw = lake_cache.load_manifest()
                if raw is None:
                    raise
                logger.info("lake manifest read from the local copy (S3: %s)", type(exc).__name__)
        else:
            return None
        files = json.loads(raw).get("files")
        return files if isinstance(files, list) else None
    except Exception as exc:
        logger.warning("lake manifest unavailable: %s", exc)
        return None


def _files() -> Optional[dict[str, dict]]:
    """Manifest entries by original path, cached for ten minutes."""
    if _cache["files"] is not None and time.time() - _cache["at"] < _TTL:
        return _cache["files"]
    entries = _read_manifest()
    if entries is None:
        return None
    by_path = {e["path"]: e for e in entries
               if isinstance(e, dict) and e.get("path") and e.get("key")}
    _cache.update(at=time.time(), files=by_path)
    return by_path


def _unavailable() -> JSONResponse:
    return JSONResponse({"error": "the data lake copy is not published on this server"},
                        status_code=503)


async def _index(request: Request) -> Response:
    if (denied := lake_auth.guard(request)) is not None:
        return denied
    files = await anyio.to_thread.run_sync(_files)
    if files is None:
        return _unavailable()
    rows = [{"path": p, "size": e.get("size"), "sha256": e.get("sha256")}
            for p, e in sorted(files.items())]
    return JSONResponse({"n": len(rows), "bytes": sum(r["size"] or 0 for r in rows),
                         "files": rows})


def _local_file(key: str) -> Optional[Path]:
    root = Path(os.environ["SURIMI_LAKE_DIR"]).resolve()
    p = (root / key).resolve()
    return p if root in p.parents and p.is_file() else None


def _s3_body(bucket: str, key: str, rng: Optional[str]):
    kw = {"Range": rng} if rng else {}
    obj = _s3_client().get_object(Bucket=bucket, Key=key, **kw)
    return obj["Body"].iter_chunks(chunk_size=1 << 20), obj.get("ContentLength")


def _parse_range(header: Optional[str], size: Optional[int]):
    m = _RANGE.match(header or "")
    if not m or size is None:
        return None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    return (start, min(end, size - 1)) if start <= end and start < size else None


async def _download(request: Request) -> Response:
    """Send one manifest file, from the local directory, the volume copy or S3, with range support for S3."""
    if (denied := lake_auth.guard(request)) is not None:
        return denied
    files = await anyio.to_thread.run_sync(_files)
    if files is None:
        return _unavailable()
    path = request.path_params["path"]
    entry = files.get(path)
    if entry is None or ".." in path.split("/"):
        return Response("not a file in the data lake copy", status_code=404)
    name = path.rsplit("/", 1)[-1]
    media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if os.environ.get("SURIMI_LAKE_DIR"):
        local = _local_file(entry["key"])
        if local is None:
            return Response("not a file in the data lake copy", status_code=404)
        return FileResponse(local, media_type=media, filename=name)
    if (copy := lake_cache.local_file(entry)) is not None:
        return FileResponse(copy, media_type=media, filename=name)
    parts = _s3_parts()
    size = entry.get("size")
    rng = _parse_range(request.headers.get("range"), size)
    headers = {"content-disposition": f'attachment; filename="{name}"', "accept-ranges": "bytes"}
    base = entry.get("prefix") or parts[1]
    key = f"{base.strip('/')}/{entry['key']}" if base else entry["key"]
    try:
        body, length = await anyio.to_thread.run_sync(
            _s3_body, parts[0], key, f"bytes={rng[0]}-{rng[1]}" if rng else None)
    except Exception as exc:
        logger.warning("lake fetch failed for %s: %s", key, exc)
        return Response("file unavailable", status_code=404)
    if length is not None:
        headers["content-length"] = str(length)
    if rng:
        headers["content-range"] = f"bytes {rng[0]}-{rng[1]}/{size}"
    return StreamingResponse(body, status_code=206 if rng else 200, media_type=media,
                             headers=headers)


def make_lake_routes() -> list[Route]:
    """Return the `/sources/lake` index route and the download route."""
    return [Route("/sources/lake", _index, methods=["GET"]),
            Route("/sources/lake/{path:path}", _download, methods=["GET"])]
