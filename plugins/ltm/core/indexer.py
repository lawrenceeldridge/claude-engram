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

from core.chunking import split_markdown
from core.config import Config
from core.distill import get_distiller
from core.embedding import EmbeddingGateway
from core.project import Project
from core.quantize import quantize_int8
from core.store import Store

_DOC_EXTENSIONS = {".md", ".markdown", ".mdx", ".mdc"}
_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next",
    "target", ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages", ".tox",
    ".idea", ".vscode", "coverage", ".turbo",
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
            if Path(name).suffix.lower() in _DOC_EXTENSIONS:
                found.append(Path(dirpath) / name)
    return found


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


def index_project(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    root: str | Path,
    *,
    summarize: bool = False,
) -> dict:
    """Index (or incrementally refresh) the markdown docs under ``root`` for a project."""
    root = Path(root)
    distiller = get_distiller(cfg) if summarize else None
    seen: set[str] = set()
    now = time.time()
    stats = {"files": 0, "skipped": 0, "chunks": 0, "deleted": 0}

    for path in _discover(root):
        try:
            source_path = str(path.relative_to(root))
            mtime_ns = path.stat().st_mtime_ns
        except (OSError, ValueError):
            continue
        seen.add(source_path)

        prior = store.source_state(project["key"], source_path)
        if prior is not None and prior[1] == mtime_ns:
            stats["skipped"] += 1
            continue  # mtime unchanged — fast path, no read

        try:
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) > _MAX_FILE_BYTES:
            stats["skipped"] += 1
            continue
        file_hash = _file_hash(data)
        if prior is not None and prior[0] == file_hash:
            stats["skipped"] += 1
            continue  # content identical despite mtime touch — nothing to re-embed

        text = data.decode("utf-8", "ignore")
        chunks = _build_chunks(store, embedder, distiller, project, source_path, text)
        store.replace_source_chunks(project["key"], source_path, chunks, file_hash, mtime_ns, now)
        stats["files"] += 1
        stats["chunks"] += len(chunks)

    for gone in store.indexed_sources(project["key"]) - seen:
        store.delete_source(project["key"], gone)
        stats["deleted"] += 1

    return stats


def _build_chunks(
    store: Store,
    embedder: EmbeddingGateway,
    distiller,
    project: Project,
    source_path: str,
    text: str,
) -> list[dict]:
    stem = Path(source_path).stem
    records: list[dict] = []
    for section in split_markdown(text, stem):
        if not section.body.strip():
            continue
        summary = _section_summary(section.title, section.body)
        if distiller is not None:
            summary = _llm_summary(distiller, section.heading_path, section.body) or summary
        vec = embedder.embed_one(_embed_text(section.title, section.heading_path, summary, section.body))
        blob, scale = quantize_int8(vec)
        records.append(
            {
                "id": store.chunk_id(project["key"], source_path, section.slug),
                "kind": "doc_section",
                "anchor": section.slug,
                "title": section.title,
                "heading_path": section.heading_path,
                "level": section.level,
                "summary": summary,
                "body": section.body,
                "byte_start": section.byte_start,
                "byte_end": section.byte_end,
                "content_hash": hashlib.sha256(section.body.encode()).hexdigest(),
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
