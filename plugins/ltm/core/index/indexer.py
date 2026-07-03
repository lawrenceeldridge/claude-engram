"""Index a project's documentation into the chunk store (discover → split → embed).

Runs off the interactive path (same as capture). For each markdown file it computes
a mtime→hash short-circuit so an unchanged file costs one ``stat`` and nothing else;
only new or edited files are re-split, re-summarised and re-embedded. Files that have
vanished since the last index are dropped. Doc sections are the retrieval unit —
embedded on ``title + heading path + summary + body head`` — and the per-section
``content_hash`` later drives freshness verification at recall time.

Summaries default to a cheap deterministic first-line extract; an LLM summary via the
distiller is opt-in (``summarize=True``) because summarising every section of a large
tree is slow and rarely worth it for ranking.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from core.config import Config
from core.domain.quantize import quantize_int8
from core.index.chunking import split_markdown
from core.index.code_symbols import extract_code_symbols
from core.ports.distill import get_distiller
from core.ports.embedding import EmbeddingGateway
from core.project import Project
from core.store import Store

_DOC_EXTENSIONS = {".md", ".markdown", ".mdx", ".mdc"}
_CODE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_INDEX_EXTENSIONS = _DOC_EXTENSIONS | _CODE_EXTENSIONS
_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".next",
    "target",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "site-packages",
    ".tox",
    ".idea",
    ".vscode",
    "coverage",
    ".turbo",
}
_MAX_FILE_BYTES = 2_000_000  # skip pathological/generated docs; nothing useful to recall
_EMBED_BODY_CHARS = 1000
_SUMMARY_CHARS = 200


def _discover(root: Path) -> list[Path]:
    """Markdown files under root, skipping vendored/build directories (in place, cheap)."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix.lower() in _INDEX_EXTENSIONS:
                found.append(Path(dirpath) / name)
    return found


def tree_signature(root: str | Path) -> int:
    """A cheap fingerprint of the indexable file tree (paths + mtime + size), no reads.

    Merkle-rollup shortcut: if this hasn't changed since the last index, nothing was
    added/removed/edited out-of-band, so the auto-index can skip the whole pass —
    including loading the embedding model. Content hashing (the exact, per-file
    freshness check) still happens inside index_project when the tree *has* changed.
    Returns a non-negative 63-bit int (fits the capture_cursors INTEGER column).
    """
    parts = []
    for path in sorted(_discover(Path(root))):
        try:
            st = path.stat()
        except OSError:
            continue
        parts.append(f"{path}|{st.st_mtime_ns}|{st.st_size}")
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
    return int(digest[:16], 16) & 0x7FFFFFFFFFFFFFFF


def _file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _section_summary(title: str, body: str) -> str:
    """Cheap deterministic summary: the first non-heading prose line, else the title."""
    for line in body.split("\n"):
        line = line.strip().lstrip("#").strip()
        if line and not line.startswith(("```", "|", ">", "-", "*")):
            return line[:_SUMMARY_CHARS]
    return title[:_SUMMARY_CHARS]


def _embed_text(title: str, heading_path: str, summary: str, body: str) -> str:
    return f"{heading_path}\n{summary}\n{body[:_EMBED_BODY_CHARS]}".strip() or title


def _index_path(
    store: Store,
    embedder: EmbeddingGateway,
    distiller,
    project: Project,
    project_root: Path,
    root: Path,
    path: Path,
    now: float,
) -> tuple[str, int, str | None]:
    """Index one file with the mtime→hash short-circuit. Returns (status, n_chunks, source_path)."""
    try:
        base = project_root if path.is_relative_to(project_root) else root
        source_path = str(path.relative_to(base))
        mtime_ns = path.stat().st_mtime_ns
    except (OSError, ValueError):
        return ("error", 0, None)

    prior = store.source_state(project["key"], source_path)
    if prior is not None and prior[1] == mtime_ns:
        return ("skipped", 0, source_path)  # mtime unchanged — no read
    try:
        data = path.read_bytes()
    except OSError:
        return ("error", 0, source_path)
    if len(data) > _MAX_FILE_BYTES:
        return ("skipped", 0, source_path)
    file_hash = _file_hash(data)
    if prior is not None and prior[0] == file_hash:
        return ("skipped", 0, source_path)  # content identical despite mtime touch

    text = data.decode("utf-8", "ignore")
    chunks = _build_chunks(store, embedder, distiller, project, source_path, text)
    store.replace_source_chunks(project["key"], source_path, chunks, file_hash, mtime_ns, now)
    return ("indexed", len(chunks), source_path)


def index_file(store: Store, embedder: EmbeddingGateway, cfg: Config, project: Project, file_path: str | Path) -> dict:
    """Re-index a single file (for the PostToolUse per-edit refresh).

    A no-LLM, hash-short-circuited single-file update — cheap enough to run on every
    Edit/Write. If the file was deleted or is no longer index-eligible, its chunks are
    dropped so the index never serves a symbol/section that has gone.
    """
    # Resolve both sides: an edited path from the tool call and the project root can
    # differ only by a symlink prefix (e.g. macOS /var → /private/var), which would
    # otherwise defeat relative_to and mis-store the source path.
    path = Path(file_path).resolve()
    project_root = Path(project["path"]).resolve() if project.get("path") else path.parent
    try:
        source_path = str(path.relative_to(project_root)) if path.is_relative_to(project_root) else None
    except (OSError, ValueError):
        source_path = None

    if path.suffix.lower() not in _INDEX_EXTENSIONS or not path.exists():
        if source_path is not None:
            store.delete_source(project["key"], source_path)
        return {"status": "removed" if source_path else "ignored", "chunks": 0}

    status, n, _sp = _index_path(store, embedder, None, project, project_root, path.parent, path, time.time())
    return {"status": status, "chunks": n}


def index_project(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    root: str | Path,
    *,
    summarize: bool = False,
    max_files: int | None = None,
) -> dict:
    """Index (or incrementally refresh) the code/docs under ``root`` for a project.

    ``max_files`` bounds an *unattended* run: if the tree has more eligible files than
    the cap the index is skipped whole, so auto-indexing a huge monorepo git-root never
    turns into a runaway embed. An explicit, user-scoped index passes None (unbounded).
    """
    root = Path(root)
    # Store paths relative to the PROJECT root (git root), not the index root, so a
    # scoped subtree index (e.g. one app in a monorepo) still resolves against the
    # project path at recall/freshness time. Falls back to the index root for files
    # somehow outside the project.
    project_root = Path(project["path"]) if project.get("path") else root
    distiller = get_distiller(cfg) if summarize else None
    seen: set[str] = set()
    now = time.time()
    stats = {"files": 0, "skipped": 0, "chunks": 0, "deleted": 0}

    discovered = _discover(root)
    if max_files is not None and len(discovered) > max_files:
        return {**stats, "skipped_too_large": len(discovered), "max_files": max_files}

    for path in discovered:
        status, n_chunks, source_path = _index_path(store, embedder, distiller, project, project_root, root, path, now)
        if source_path is not None:
            seen.add(source_path)
        if status == "indexed":
            stats["files"] += 1
            stats["chunks"] += n_chunks
        elif status == "skipped":
            stats["skipped"] += 1

    for gone in store.indexed_sources(project["key"]) - seen:
        store.delete_source(project["key"], gone)
        stats["deleted"] += 1

    return stats


def _doc_units(source_path: str, text: str) -> list[dict]:
    """Normalise markdown sections into index units."""
    units = []
    for s in split_markdown(text, Path(source_path).stem):
        if not s.body.strip():
            continue
        units.append(
            {
                "kind": "doc_section",
                "anchor": s.slug,
                "title": s.title,
                "heading_path": s.heading_path,
                "level": s.level,
                "body": s.body,
                "byte_start": s.byte_start,
                "byte_end": s.byte_end,
                "summary": _section_summary(s.title, s.body),
            }
        )
    return units


def _code_units(text: str, ext: str) -> list[dict]:
    """Normalise code symbols into index units. Anchor is the dotted qualname."""
    units = []
    for sym in extract_code_symbols(text, ext):
        if not sym.body.strip():
            continue
        summary = f"{sym.signature} — {sym.docstring}" if sym.docstring else sym.signature
        units.append(
            {
                "kind": "code_symbol",
                "anchor": sym.qualname,
                "title": sym.name,
                "heading_path": sym.qualname,
                "level": sym.level,
                "body": sym.body,
                "byte_start": sym.byte_start,
                "byte_end": sym.byte_end,
                "summary": summary[:_SUMMARY_CHARS],
            }
        )
    return units


def _build_chunks(
    store: Store,
    embedder: EmbeddingGateway,
    distiller,
    project: Project,
    source_path: str,
    text: str,
) -> list[dict]:
    ext = Path(source_path).suffix.lower()
    is_code = ext in _CODE_EXTENSIONS
    units = _code_units(text, ext) if is_code else _doc_units(source_path, text)
    records: list[dict] = []
    seen_anchors: dict[str, int] = {}
    for unit in units:
        anchor = unit["anchor"]  # disambiguate overloads / duplicate names within a file
        if anchor in seen_anchors:
            seen_anchors[anchor] += 1
            anchor = f"{anchor}~{seen_anchors[anchor]}"
        else:
            seen_anchors[anchor] = 1
        unit["anchor"] = anchor
        summary = unit["summary"]
        if distiller is not None and not is_code:  # LLM summaries only add value for prose
            summary = _llm_summary(distiller, unit["heading_path"], unit["body"]) or summary
        vec = embedder.embed_one(_embed_text(unit["title"], unit["heading_path"], summary, unit["body"]))
        blob, scale = quantize_int8(vec)
        records.append(
            {
                "id": store.chunk_id(project["key"], source_path, unit["anchor"]),
                "kind": unit["kind"],
                "anchor": unit["anchor"],
                "title": unit["title"],
                "heading_path": unit["heading_path"],
                "level": unit["level"],
                "summary": summary,
                "body": unit["body"],
                "byte_start": unit["byte_start"],
                "byte_end": unit["byte_end"],
                "content_hash": hashlib.sha256(unit["body"].encode()).hexdigest(),
                "dim": len(vec),
                "scale": scale,
                "vec_int8": blob,
            }
        )
    return records


def _llm_summary(distiller, heading_path: str, body: str) -> str:
    """Best-effort one-line LLM summary; falls back silently so indexing never breaks."""
    try:
        fact = distiller.summarize(f"Section: {heading_path}\n\n{body[:2000]}")
        return fact.title[:_SUMMARY_CHARS] if fact and fact.title else ""
    except Exception:
        return ""
