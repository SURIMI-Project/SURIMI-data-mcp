#!/usr/bin/env python3
"""Upload the SURIMI data lake or the docs corpus to the project-surimi bucket on MinIO.

The script reads the `AWS_*` variables and `AWS_ENDPOINT_URL` from the environment. It uploads
in parallel, uses multipart upload for large files and overwrites keys that already exist.
Keys are sanitized to lower_snake_case.

Usage:
    set -a; . ~/.surimi-s3.env; set +a
    python scripts/upload_lake_to_minio.py data    # lake -> surimi_data/
    python scripts/upload_lake_to_minio.py docs    # corpus -> surimi_docs/
"""
import concurrent.futures as cf
import os
import re
import sys
import threading
import unicodedata
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig

BUCKET = os.environ.get("SURIMI_BUCKET", "project-surimi")
CFG = TransferConfig(multipart_threshold=8 * 1024 * 1024, max_concurrency=4,
                     multipart_chunksize=16 * 1024 * 1024)

# The defaults are in the references/ folder of the SURIMI monorepo, which holds this server
# under surimi-mcp/. Outside the monorepo, set SURIMI_LAKE_ROOT and SURIMI_WIKI_ROOT.
REFERENCES = Path(__file__).resolve().parents[2] / "references"
LAKE = os.environ.get("SURIMI_LAKE_ROOT", str(REFERENCES / "SURIMI-DATA-LAKE"))

# The docs corpus is the text that the server searches. It holds only public material:
# the SURIMI project wiki here and the data-lake READMEs added in `upload_docs`. The internal
# development wiki is not uploaded.
DOC_DIRS = [
    (os.environ.get("SURIMI_WIKI_ROOT", str(REFERENCES / "SURIMI-project.wiki")),
     "surimi_project_wiki"),
]
DOC_EXTS = {".md", ".txt", ".rst", ".pdf", ".mediawiki", ".proto"}

_local = threading.local()


def _client():
    if not hasattr(_local, "c"):
        endpoint = os.environ.get("AWS_ENDPOINT_URL")
        if not endpoint:
            raise SystemExit("AWS_ENDPOINT_URL not set (source ~/.surimi-s3.env first)")
        _local.c = boto3.client(
            "s3", endpoint_url=endpoint,
            region_name=os.environ.get("AWS_DEFAULT_REGION"),
        )
    return _local.c


def sanitize_key_segment(segment: str) -> str:
    """Turn one S3 key path segment into lower_snake_case.

    The segment is normalised with NFKD, "&" becomes "_and_" and every other run of characters that are not letters or digits becomes one underscore.
    """
    s = unicodedata.normalize("NFKD", segment)
    s = s.replace("&", " and ")
    s = re.sub(r"[^0-9a-zA-Z]+", "_", s).strip("_").lower()
    return s


def sanitize_relative_key(rel: Path) -> str:
    """Sanitize every directory segment and the file name stem of a relative path, and keep the file extension in lowercase."""
    parts = [sanitize_key_segment(p) for p in rel.parts[:-1]]
    name = rel.parts[-1]
    stem, suffix = Path(name).stem, Path(name).suffix
    parts.append(sanitize_key_segment(stem) + suffix.lower())
    return "/".join(parts)


def _upload_files(files, root, prefix):
    """Upload ``files`` under ``prefix``, with keys built from their paths relative to ``root``, and return the number uploaded and the list of (key, error)."""
    root = Path(root)
    total, done, errs = len(files), [0], []
    lock = threading.Lock()

    def up(p):
        key = prefix.rstrip("/") + "/" + sanitize_relative_key(Path(p).relative_to(root))
        try:
            _client().upload_file(str(p), BUCKET, key, Config=CFG)
            return (key, None)
        except Exception as e:  # noqa: BLE001
            return (key, str(e))

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for key, err in ex.map(up, files):
            with lock:
                done[0] += 1
                if err:
                    errs.append((key, err))
                if done[0] % 10 == 0 or done[0] == total:
                    print(f"  {done[0]}/{total}", flush=True)
    print(f"DONE {prefix}: {total - len(errs)}/{total} uploaded, {len(errs)} errors", flush=True)
    for k, e in errs[:8]:
        print("  ERR", k, "->", e[:160], flush=True)
    return total - len(errs), errs


def upload_data():
    """Upload the .csv, .xlsx and .txt files of the lake to ``surimi_data/``."""
    root = Path(LAKE)
    files = [p for p in root.rglob("*") if p.is_file() and "/.git/" not in str(p)
             and p.suffix.lower() in {".csv", ".xlsx", ".txt"}]
    sz = sum(p.stat().st_size for p in files)
    print(f"=== lake -> surimi_data/ : {len(files)} files, {sz/1e9:.2f} GB ===", flush=True)
    return _upload_files(files, root, "surimi_data")


def upload_docs():
    """Upload the docs corpus to ``surimi_docs/`` and the lake READMEs to ``surimi_docs/data_lake/``, and return the number of files uploaded."""
    ok = 0
    for root, prefix in DOC_DIRS:
        rp = Path(root)
        if not rp.is_dir():
            continue
        files = [p for p in rp.rglob("*") if p.is_file() and "/.git/" not in str(p)
                 and p.suffix.lower() in DOC_EXTS]
        print(f"=== {root} -> surimi_docs/{prefix}/ : {len(files)} files ===", flush=True)
        n, _ = _upload_files(files, rp, f"surimi_docs/{prefix}")
        ok += n
    # the READMEs of the data lake
    rp = Path(LAKE)
    readmes = [p for p in rp.rglob("*") if p.is_file()
               and p.name.lower().startswith("readme") and p.suffix.lower() in {".txt", ".md"}]
    print(f"=== data-lake READMEs -> surimi_docs/data_lake/ : {len(readmes)} files ===", flush=True)
    n, _ = _upload_files(readmes, rp, "surimi_docs/data_lake")
    ok += n
    print(f"TOTAL docs uploaded: {ok}", flush=True)
    return ok


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "data"
    if what == "data":
        upload_data()
    elif what == "docs":
        upload_docs()
    else:
        print("usage: upload_lake_to_minio.py [data|docs]")
