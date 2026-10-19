#!/usr/bin/env python3
"""Upload the SURIMI data lake to <bucket>/mcp_data/data/ and write its lake_manifest.json.

The data server serves this copy at /sources/lake/<original path> (see lake_routes.py). The
manifest lists each file with its original path, its key, its size and its sha256, so a script
written against the local lake can fetch the same file by its original path.

The script writes only under `mcp_data/` and refuses any other prefix. An object whose size and
sha256 already match is skipped. The manifest is written last, so an interrupted run does not publish a new manifest.
With `--keys original` (the default) the key is the lake's own path, and with `--keys sanitized` it is lower_snake_case.

    set -a; . ~/.surimi-s3.env; set +a      # AWS_* and AWS_ENDPOINT_URL, a token that can write mcp_data/
    python scripts/publish_lake_copy.py --dry-run        # plan only, no network
    python scripts/publish_lake_copy.py --check          # read-only: which keys exist, which sizes differ
    python scripts/publish_lake_copy.py --manifest-only  # keys already there: write just the manifest
    python scripts/publish_lake_copy.py --fill           # upload what is missing or different to the overlay prefix, then the manifest
    python scripts/publish_lake_copy.py                  # upload every file that is not already there, then the manifest
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from upload_lake_to_minio import sanitize_relative_key  # noqa: E402

# The references/ folder of the SURIMI monorepo, which holds this server under surimi-mcp/.
# Outside the monorepo, pass --lake or set SURIMI_LAKE_ROOT.
DEFAULT_LAKE = str(Path(__file__).resolve().parents[2] / "references" / "SURIMI-DATA-LAKE")
_local = threading.local()


def check_prefix(prefix: str) -> str:
    """Return ``prefix`` without surrounding slashes, and exit when it is not under ``mcp_data/`` or contains ``..``."""
    p = prefix.strip("/")
    if not p.startswith("mcp_data/") or ".." in p.split("/"):
        raise SystemExit(f"refusing to write to {prefix!r}: only mcp_data/<something> is ours")
    return p


def _sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of the file at ``path``."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def plan(lake: Path, workers: int = 8, key_style: str = "original") -> list[dict]:
    """Return one entry (path, key, size, sha256) for each file in the lake, skipping hidden files and folders.

    With key_style "original" the key is the file's path in the lake, and with "sanitized" it is a lower_snake_case key.
    The function exits when two files map to the same key.
    """
    lake = Path(lake)
    files = sorted(p for p in lake.rglob("*")
                   if p.is_file() and not any(part.startswith(".") for part in p.relative_to(lake).parts))
    rels = [p.relative_to(lake) for p in files]
    keys = [r.as_posix() if key_style == "original" else sanitize_relative_key(r) for r in rels]
    seen: dict[str, str] = {}
    for rel, key in zip(rels, keys):
        if key in seen:
            raise SystemExit(f"{rel.as_posix()} and {seen[key]} map to the same key {key}")
        seen[key] = rel.as_posix()
    with cf.ThreadPoolExecutor(workers) as ex:
        sums = list(ex.map(_sha256, files))
    return [{"path": r.as_posix(), "key": k, "size": p.stat().st_size, "sha256": s}
            for r, k, p, s in zip(rels, keys, files, sums)]


def manifest_json(entries: list[dict]) -> str:
    """Return the text of lake_manifest.json for the given entries."""
    return json.dumps({"files": entries}, indent=1, ensure_ascii=False) + "\n"


def _client():
    """Return the S3 client of the current thread for ``AWS_ENDPOINT_URL``, and exit when that is unset."""
    import boto3
    if not hasattr(_local, "c"):
        endpoint = os.environ.get("AWS_ENDPOINT_URL")
        if not endpoint:
            raise SystemExit("AWS_ENDPOINT_URL not set")
        _local.c = boto3.client("s3", endpoint_url=endpoint,
                                region_name=os.environ.get("AWS_DEFAULT_REGION"))
    return _local.c


def _already_there(bucket: str, key: str, entry: dict) -> bool:
    """Return True when the object exists with the size and sha256 metadata of ``entry``."""
    try:
        head = _client().head_object(Bucket=bucket, Key=key)
    except Exception:
        return False
    return head["ContentLength"] == entry["size"] and head.get("Metadata", {}).get("sha256") == entry["sha256"]


def audit(bucket: str, prefix: str, entries: list[dict], workers: int = 8) -> dict:
    """Compare each planned key with the bucket without writing, and return the paths grouped as same, missing and differs by size."""
    def one(e):
        try:
            head = _client().head_object(Bucket=bucket, Key=f"{prefix}/{e['key']}")
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
            return ("missing" if code in ("404", "NoSuchKey", "NotFound") else f"error {code}", e)
        return ("same" if head["ContentLength"] == e["size"] else "differs", e)
    out: dict[str, list] = {"same": [], "missing": [], "differs": []}
    with cf.ThreadPoolExecutor(workers) as ex:
        for kind, e in ex.map(one, entries):
            out.setdefault(kind, []).append(e["path"])
    return out


def _put(args: tuple) -> str:
    """Upload one entry unless it is already there, and return ``skip`` or ``put``."""
    lake, bucket, prefix, entry = args
    key = f"{prefix}/{entry['key']}"
    if _already_there(bucket, key, entry):
        return "skip"
    from boto3.s3.transfer import TransferConfig
    cfg = TransferConfig(multipart_threshold=8 << 20, max_concurrency=4, multipart_chunksize=16 << 20)
    _client().upload_file(str(Path(lake) / entry["path"]), bucket, key, Config=cfg,
                          ExtraArgs={"Metadata": {"sha256": entry["sha256"]}})
    return "put"


def main(argv: list[str] | None = None) -> None:
    """Run the upload, check or manifest step chosen by the command-line options."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--lake", default=os.environ.get("SURIMI_LAKE_ROOT", DEFAULT_LAKE))
    ap.add_argument("--bucket", default=os.environ.get("SURIMI_BUCKET", "project-surimi"))
    ap.add_argument("--prefix", default="mcp_data/data")
    ap.add_argument("--keys", choices=["original", "sanitized"], default="original",
                    help="original: the lake's own paths, as already stored in mcp_data/data")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true", help="read-only audit against the bucket")
    ap.add_argument("--overlay-prefix", default="mcp_data/lake_local",
                    help="--fill puts files that are missing or differ here, never over mcp_data/data")
    ap.add_argument("--fill", action="store_true",
                    help="upload what is missing or different to the overlay prefix, then write the manifest")
    ap.add_argument("--manifest-only", action="store_true",
                    help="every key is already there: verify, then write only the manifest")
    a = ap.parse_args(argv)
    prefix = check_prefix(a.prefix)
    overlay = check_prefix(a.overlay_prefix)
    if overlay == prefix or overlay.startswith(prefix + "/") or prefix.startswith(overlay + "/"):
        raise SystemExit("the overlay prefix must be separate from the main one")
    entries = plan(Path(a.lake), key_style=a.keys)
    total = sum(e["size"] for e in entries)
    print(f"{len(entries)} files, {total / 1e9:.2f} GB from {a.lake} -> s3://{a.bucket}/{prefix}/")
    if a.dry_run:
        print("dry run: nothing written")
        return
    if a.check or a.manifest_only:
        res = audit(a.bucket, prefix, entries)
        print("  " + "  ".join(f"{k} {len(v)}" for k, v in res.items()))
        for kind in [k for k in res if k != "same"]:
            for path in res[kind][:20]:
                print(f"  {kind}: {path}")
        if a.check:
            return
        if any(v for k, v in res.items() if k != "same"):
            raise SystemExit("not writing the manifest: the bucket does not match the local lake")
        _client().put_object(Bucket=a.bucket, Key=f"{prefix}/lake_manifest.json",
                             Body=manifest_json(entries).encode("utf-8"), ContentType="application/json")
        print(f"manifest written: s3://{a.bucket}/{prefix}/lake_manifest.json")
        return
    if a.fill:
        res = audit(a.bucket, prefix, entries)
        odd = {k for k in res if k not in ("same", "missing", "differs")}
        if odd:
            raise SystemExit(f"not filling: the bucket answered {sorted(odd)}, which is not a plain absence")
        move = set(res["missing"]) | set(res["differs"])
        for e in entries:
            if e["path"] in move:
                e["prefix"] = overlay
        todo = [e for e in entries if e["path"] in move]
        print(f"  {len(res['same'])} already in {prefix}; {len(todo)} go to {overlay} "
              f"({sum(e['size'] for e in todo) / 1e9:.2f} GB)")
        done = {"put": 0, "skip": 0}
        with cf.ThreadPoolExecutor(a.workers) as ex:
            for i, r in enumerate(ex.map(_put, [(a.lake, a.bucket, overlay, e) for e in todo]), 1):
                done[r] += 1
                print(f"  {i}/{len(todo)}", flush=True) if i % 10 == 0 or i == len(todo) else None
        _client().put_object(Bucket=a.bucket, Key=f"{prefix}/lake_manifest.json",
                             Body=manifest_json(entries).encode("utf-8"), ContentType="application/json")
        print(f"manifest written: s3://{a.bucket}/{prefix}/lake_manifest.json "
              f"({len(entries)} files; uploaded {done['put']}, already there {done['skip']})")
        return
    done = {"put": 0, "skip": 0}
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for i, r in enumerate(ex.map(_put, [(a.lake, a.bucket, prefix, e) for e in entries]), 1):
            done[r] += 1
            if i % 25 == 0 or i == len(entries):
                print(f"  {i}/{len(entries)}  uploaded {done['put']}  already there {done['skip']}", flush=True)
    _client().put_object(Bucket=a.bucket, Key=f"{prefix}/lake_manifest.json",
                         Body=manifest_json(entries).encode("utf-8"), ContentType="application/json")
    print(f"manifest written: s3://{a.bucket}/{prefix}/lake_manifest.json")


if __name__ == "__main__":
    main()
