"""Reads the sources manifest and the cube hash that a receipt's lineage comes from.

`scripts/build_sources_manifest.py` writes `sources_manifest.json`. The server looks for it at
`SURIMI_SOURCES_MANIFEST`, or next to the file named by `SURIMI_CUBE_DB`. When the manifest is absent
or unreadable, the functions return None or an empty dict, so lineage is never made up. The cube
sha256 is computed once per process and kept for the server's lifetime.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

_MANIFEST: Optional[dict[str, Any]] = None
_MANIFEST_LOADED = False
_CUBE_SHA: Optional[str] = None
_CUBE_SHA_DONE = False
_CUBE_SHA_LOCK = threading.Lock()


def reset() -> None:
    """Drop the cached manifest and cube hash. Tests call it."""
    global _MANIFEST, _MANIFEST_LOADED, _CUBE_SHA, _CUBE_SHA_DONE
    _MANIFEST, _MANIFEST_LOADED = None, False
    _CUBE_SHA, _CUBE_SHA_DONE = None, False


def _manifest_path() -> Optional[str]:
    env = os.environ.get("SURIMI_SOURCES_MANIFEST")
    if env:
        return env
    cube_db = os.environ.get("SURIMI_CUBE_DB", "")
    if cube_db and cube_db != ":memory:":
        return str(Path(cube_db).parent / "sources_manifest.json")
    return None


def _load() -> Optional[dict[str, Any]]:
    global _MANIFEST, _MANIFEST_LOADED
    if _MANIFEST_LOADED:
        return _MANIFEST
    _MANIFEST_LOADED = True
    path = _manifest_path()
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                _MANIFEST = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logging.getLogger(__name__).warning(
                "sources manifest at %s is unreadable (%s); serving without lineage",
                path, exc,
            )
            _MANIFEST = None     # unreadable manifest == no lineage, never crash
    return _MANIFEST


def manifest_loaded() -> bool:
    """Return True when a manifest was read."""
    return _load() is not None


def source_base_url() -> str:
    """Return `SURIMI_SOURCE_BASE_URL` without a trailing slash, or an empty string."""
    return os.environ.get("SURIMI_SOURCE_BASE_URL", "").rstrip("/")


def get_source_for(dataset: str) -> Optional[dict[str, Any]]:
    """Return the manifest entry for a cube dataset, or None.

    A download `url` is added when `SURIMI_SOURCE_BASE_URL` is set and the entry has a `file`.
    """
    m = _load()
    entry = (m or {}).get(dataset)
    if not entry:
        return None
    out = dict(entry)
    base = source_base_url()
    if base and entry.get("file"):
        out["url"] = f"{base}/{entry['file']}"
    return out


def all_sources() -> dict[str, Any]:
    """Return a copy of the whole manifest for the `/sources` index, or an empty dict when there is none."""
    return dict(_load() or {})


def get_cube_sha256() -> Optional[str]:
    """Return the sha256 of the file named by `SURIMI_CUBE_DB`, or None when it is unset, `:memory:` or missing.

    The hash is computed once per process, under a lock.
    """
    global _CUBE_SHA, _CUBE_SHA_DONE
    if _CUBE_SHA_DONE:
        return _CUBE_SHA
    with _CUBE_SHA_LOCK:
        if _CUBE_SHA_DONE:           # double-checked: another thread finished first
            return _CUBE_SHA
        cube_db = os.environ.get("SURIMI_CUBE_DB", "")
        if cube_db and cube_db != ":memory:" and os.path.exists(cube_db):
            h = hashlib.sha256()
            with open(cube_db, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            _CUBE_SHA = h.hexdigest()
        _CUBE_SHA_DONE = True        # only after _CUBE_SHA is assigned
        return _CUBE_SHA
