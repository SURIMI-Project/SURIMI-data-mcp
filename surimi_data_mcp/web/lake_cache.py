"""Keeps a copy of the data lake on the server's own volume, so `/sources/lake` still works after the daily S3 token expires.

EDITO's S3 tokens last about a day. `warm()` copies every file listed in `lake_manifest.json` to
`SURIMI_LAKE_CACHE`, under `files/<original path>`, checks each one against its sha256 and saves the
manifest there too. `lake_routes` serves the local copy of a file when it is complete and reads S3 for
any file not yet copied.

`warm()` copies only what is missing, so a relaunch costs nothing once the copy is complete.
`start_background()` runs it in a daemon thread at server start and, while files are still missing
(S3 refused, for example), tries again every six hours. Nothing here writes outside `SURIMI_LAKE_CACHE`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

RETRY_SECONDS = 6 * 3600
_lock = threading.Lock()


def cache_dir() -> Optional[Path]:
    """Return the directory named by `SURIMI_LAKE_CACHE`, or None when it is unset."""
    d = os.environ.get("SURIMI_LAKE_CACHE", "").strip()
    return Path(d).resolve() if d else None


def _target(root: Path, path: str) -> Optional[Path]:
    files = (root / "files").resolve()
    t = (files / path).resolve()
    return t if files in t.parents else None


def local_file(entry: dict) -> Optional[Path]:
    """Return the path of the local copy of a manifest entry, or None. A copy counts as complete when its size matches."""
    root = cache_dir()
    if not root:
        return None
    t = _target(root, entry.get("path", ""))
    if t and t.is_file() and t.stat().st_size == entry.get("size"):
        return t
    return None


def save_manifest(raw: bytes) -> None:
    """Write the manifest bytes to `lake_manifest.json` in the cache, through a `.part` file. Does nothing without a cache."""
    root = cache_dir()
    if not root:
        return
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "lake_manifest.json.part"
    tmp.write_bytes(raw)
    tmp.replace(root / "lake_manifest.json")


def load_manifest() -> Optional[bytes]:
    """Return the manifest bytes saved in the cache, or None."""
    root = cache_dir()
    p = root / "lake_manifest.json" if root else None
    return p.read_bytes() if p and p.is_file() else None


def _free_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def _fetch(entry: dict, dest: Path) -> bool:
    """Copy one S3 object to `dest` through a `.part` file and keep it only if size and sha256 match.

    Returns False on a mismatch and raises on an S3 error.
    """
    from surimi_data_mcp.web import lake_routes
    bucket, prefix = lake_routes._s3_parts()
    base = (entry.get("prefix") or prefix).strip("/")
    key = f"{base}/{entry['key']}" if base else entry["key"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    try:
        body = lake_routes._s3_client().get_object(Bucket=bucket, Key=key)["Body"]
        with open(part, "wb") as f:
            for chunk in body.iter_chunks(chunk_size=1 << 22):
                f.write(chunk)
                h.update(chunk)
        if part.stat().st_size != entry["size"] or h.hexdigest() != entry["sha256"]:
            logger.warning("lake copy: %s did not match its manifest checksum; not kept", entry["path"])
            part.unlink(missing_ok=True)
            return False
        part.replace(dest)
        return True
    except Exception as exc:
        part.unlink(missing_ok=True)
        logger.warning("lake copy: %s not copied: %s", entry["path"], type(exc).__name__)
        raise


def warm() -> Optional[dict]:
    """Copy the files that are missing and return the counts, or None when no cache is configured.

    The counts are `copied`, `present`, `failed` and `total`, and `skipped` when the free space is
    less than the missing files plus 512 MiB. An S3 error stops the run, and the next run continues.
    """
    root = cache_dir()
    if not root:
        return None
    from surimi_data_mcp.web import lake_routes
    with _lock:
        lake_routes.reset()
        entries = lake_routes._read_manifest() or []
        stats = {"copied": 0, "present": 0, "failed": 0, "total": len(entries)}
        todo = []
        for e in entries:
            t = _target(root, e.get("path", ""))
            if t is None:
                stats["failed"] += 1
            elif local_file(e):
                stats["present"] += 1
            else:
                todo.append((e, t))
        need = sum(e["size"] for e, _ in todo)
        if todo and _free_bytes(root) < need + (512 << 20):
            logger.warning("lake copy: %d bytes needed, not enough free space on %s", need, root)
            stats["skipped"] = "not enough free space"
            return stats
        for e, t in todo:
            try:
                stats["copied" if _fetch(e, t) else "failed"] += 1
            except Exception:
                stats["failed"] += 1   # S3 refused: stop here, the next run continues
                break
        logger.info("lake copy: %s", stats)
        return stats


def start_background() -> Optional[threading.Thread]:
    """Start a daemon thread that runs `warm()` every `RETRY_SECONDS` until every file is present.

    Returns the thread, or None when `SURIMI_LAKE_CACHE` is unset or `SURIMI_LAKE_S3` is not an `s3://` URI.
    """
    if not cache_dir() or not os.environ.get("SURIMI_LAKE_S3", "").startswith("s3://"):
        return None

    def loop():
        while True:
            try:
                s = warm() or {}
                if s.get("total") and s.get("present", 0) + s.get("copied", 0) == s["total"]:
                    return
            except Exception:
                logger.warning("lake copy failed; retrying later", exc_info=True)
            time.sleep(RETRY_SECONDS)

    t = threading.Thread(target=loop, name="lake-copy", daemon=True)
    t.start()
    return t
