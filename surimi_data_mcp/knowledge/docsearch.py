"""BM25 full-text search over the SURIMI document corpus, using SQLite FTS5 from the Python standard library.

The server splits each document into passages and stores them in an FTS5 table. A search returns the
best-matching passages as exact text from the source file, and returns an empty list when no query
word matches. PDF files are converted with the external `pdftotext` program, and are skipped when it
is missing.

Public API:
  build_index(corpus_dirs, db_path=":memory:") -> DocSearch
  search(query, top_k)            # module level, builds the index on first use
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Optional
from surimi_data_mcp.paths import APP_ROOT

DOC_EXTS = (".md", ".txt", ".rst", ".pdf")
MAX_CHARS = 1400          # maximum passage length in characters
MIN_CHARS = 40            # passages shorter than this are dropped
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+./-]*")
# Directories indexed by default, relative to APP_ROOT. Only those that exist are used.
# The internal development wiki ("wiki/") is not listed because it holds meeting notes,
# stakeholder notes and development logs. The public project wiki is
# references/SURIMI-project.wiki.
_DEFAULT_DIRS = (
    "references",
    "references/SURIMI-project.wiki",
)

# Files skipped in every indexed directory, including those named in SURIMI_DOCS_DIRS.
# `_is_internal_doc` skips any path with a `wiki` or `meeting-notes` segment, so the
# single segment `SURIMI-project.wiki` is not matched.
_INTERNAL_BASENAMES = {
    "stakeholders.md", "log.md", "design-doc.md", "deployment-prompt.md",
    "architecture-doc.md", "solution-overview.md", "consortium-model-repos.md",
}


def _is_internal_doc(path: Path) -> bool:
    """Return True for a path in the internal development wiki or the meeting notes, which the index skips."""
    parts = path.parts
    if "wiki" in parts or "meeting-notes" in parts:   # dev wiki / raw transcripts
        return True
    name = path.name.lower()
    return name in _INTERNAL_BASENAMES or name.startswith("meeting-")


def _read_text(path: Path) -> str:
    """Return the text of a file, running `pdftotext` for PDFs, or an empty string when it cannot be read."""
    if path.suffix.lower() == ".pdf":
        try:
            return subprocess.check_output(
                ["pdftotext", "-layout", str(path), "-"],
                stderr=subprocess.DEVNULL).decode("utf-8", errors="replace")
        except (FileNotFoundError, subprocess.CalledProcessError):
            return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _chunk(text: str) -> list[str]:
    """Split text into passages by merging paragraphs up to `MAX_CHARS` characters each.

    Paragraphs longer than `MAX_CHARS` are cut at fixed character positions, and passages shorter than `MIN_CHARS` are dropped.
    """
    blocks = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    buf = ""
    for blk in blocks:
        blk = blk.strip("\n")
        if not blk.strip():
            continue
        if len(blk) > MAX_CHARS:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(blk), MAX_CHARS):
                chunks.append(blk[i:i + MAX_CHARS])
            continue
        if buf and len(buf) + len(blk) + 2 > MAX_CHARS:
            chunks.append(buf)
            buf = blk
        else:
            buf = f"{buf}\n\n{blk}" if buf else blk
    if buf:
        chunks.append(buf)
    return [c.strip() for c in chunks if len(c.strip()) >= MIN_CHARS]


def _match_query(query: str) -> Optional[str]:
    """Turn free text into an FTS5 MATCH string of quoted tokens joined by OR.

    Quoting stops FTS operators and punctuation in the query from being interpreted. The function returns None when the query has no token of two or more characters, and keeps at most 24 distinct tokens.
    """
    tokens = [t for t in _TOKEN_RE.findall(query or "") if len(t) >= 2]
    if not tokens:
        return None
    # drop duplicates (case-insensitive) and stop at 24 tokens
    seen, uniq = set(), []
    for t in tokens:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            uniq.append(t.replace('"', ""))
        if len(uniq) >= 24:
            break
    return " OR ".join(f'"{t}"' for t in uniq)


class DocSearch:
    """An FTS5 index of document passages, with `add_dir` to fill it and `search` to query it."""

    def __init__(self, db_path: str = ":memory:"):
        # check_same_thread=False: the index is shared by the whole process and tool
        # calls run on worker threads, so search() can run on a different thread from
        # the one that built the index. The lock serialises access to the one connection.
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self.conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS docs "
            "USING fts5(text, source UNINDEXED, ordinal UNINDEXED, tokenize='porter unicode61')")

    def add_dir(self, root: str) -> int:
        """Index every `.md`, `.txt`, `.rst` and `.pdf` file under `root` and return the number of passages added.

        The method skips internal documents and returns 0 when `root` is not a directory. Each passage is stored with its path relative to the parent of `root`.
        """
        base = Path(root)
        if not base.is_dir():
            return 0
        n = 0
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in DOC_EXTS:
                continue
            if "/.git/" in str(path):
                continue
            if _is_internal_doc(path):
                continue
            rel = str(path.relative_to(base.parent)) if base.parent != base else str(path)
            for i, chunk in enumerate(_chunk(_read_text(path))):
                self.conn.execute(
                    "INSERT INTO docs(text, source, ordinal) VALUES (?,?,?)",
                    (chunk, rel, i))
                n += 1
        self.conn.commit()
        return n

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Return up to `top_k` passages ranked by BM25, each a dict with `text`, `source`, `ordinal` and `score`.

        The list is empty when the query has no usable token or the FTS5 query fails.
        """
        match = _match_query(query)
        if match is None:
            return []
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT text, source, ordinal, bm25(docs) AS score "
                    "FROM docs WHERE docs MATCH ? ORDER BY bm25(docs) LIMIT ?",
                    (match, max(1, int(top_k)))).fetchall()
        except sqlite3.OperationalError:
            return []
        return [{"text": t, "source": s, "ordinal": o, "score": round(sc, 4)}
                for (t, s, o, sc) in rows]


def build_index(corpus_dirs: list[str], db_path: str = ":memory:") -> DocSearch:
    """Create a `DocSearch` at `db_path` and index each directory in `corpus_dirs`."""
    idx = DocSearch(db_path)
    for d in corpus_dirs:
        idx.add_dir(d)
    return idx


# ── module-level index, built on first use and shared by the tools ───────────
_INDEX: Optional[DocSearch] = None
_INDEX_LOCK = threading.Lock()


def _corpus_dirs() -> list[str]:
    """Return the directories to index: `SURIMI_DOCS_DIRS` split on `os.pathsep`, else the existing `_DEFAULT_DIRS` under `APP_ROOT`."""
    env = os.environ.get("SURIMI_DOCS_DIRS")
    if env:
        return [p for p in env.split(os.pathsep) if p]
    root = APP_ROOT
    out = []
    for d in _DEFAULT_DIRS:
        p = (root / d)
        if p.is_dir():
            out.append(str(p))
    return out


def get_index() -> DocSearch:
    """Return the shared index, building it on the first call from `_corpus_dirs()` and the `SURIMI_DOCS_DB` path (default in memory)."""
    global _INDEX
    if _INDEX is None:
        # Several worker threads can make the first call at the same time, so the
        # lock and the second check make sure the index is built once.
        with _INDEX_LOCK:
            if _INDEX is None:
                db = os.environ.get("SURIMI_DOCS_DB", ":memory:")
                _INDEX = build_index(_corpus_dirs(), db_path=db)
    return _INDEX


def search(query: str, top_k: int = 5) -> list[dict]:
    """Search the shared index and return up to `top_k` passages, as `DocSearch.search` does."""
    return get_index().search(query, top_k=top_k)
