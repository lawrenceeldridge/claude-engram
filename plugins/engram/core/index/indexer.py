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
from core.domain.episodes import Exchange
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
    if status == "indexed":
        store.set_index_meta(project)  # record a human name for the index (viewer dropdown)
    return {"status": status, "chunks": n}


def _snapshot_title(url: str, text: str) -> str:
    """Best-effort title for a snapshot chunk — the URL identifies the page (the searchable
    content is the embedded body, so this stays deliberately simple)."""
    return (url or "page snapshot")[:200]


def _snapshot_summary(text: str) -> str:
    """One-line summary for a snapshot chunk: the first non-empty line of the a11y text."""
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return first[:200]


def exchange_anchor(episode: str, turn: int, part: int) -> str:
    """An exchange chunk's anchor — ``<episode>:<turn>.<part>`` (the episode key holds a ``:`` too)."""
    return f"{episode}:{turn}.{part}"


def exchange_position(anchor: str) -> tuple[int, int]:
    """``(turn, part)`` back from an exchange anchor; ``ValueError`` when it isn't one."""
    _episode, sep, position = anchor.rpartition(":")
    turn, dot, part = position.partition(".")
    if not (sep and dot):
        raise ValueError(f"not an exchange anchor: {anchor!r}")
    return int(turn), int(part)


def exchange_chunk_units(episode: str, exchanges: list[Exchange], title: str) -> list[dict]:
    """Index units for one episode's verbatim exchanges — the shape both capture and the LongMemEval
    benchmark store, so what is measured is what ships. Anchors are ``exchange_anchor``'s."""
    return [
        {
            "anchor": exchange_anchor(episode, ex.turn, ex.part),
            "kind": "exchange",
            "title": title,
            "heading_path": episode,
            "level": 0,
            "summary": ex.text.split("\n", 1)[0][:160],
            "body": ex.text,
            "byte_start": 0,
            "byte_end": len(ex.text.encode()),
        }
        for ex in exchanges
    ]


def index_snapshot(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    url: str,
    text: str,
    *,
    now: float | None = None,
) -> dict:
    """Index a page accessibility snapshot as a durable ``snapshot`` chunk — the index's visual
    long-term-store column. A-S: a visual perception transfers straight to the visual store (no
    verbal coding), so it lands in the index, never in the ``facts`` recall surface. Reuses the
    chunk pipeline (embed + FTS + get_chunk); ``source_path`` is the URL and freshness is
    age-based, not file-drift. One chunk per URL — the latest perception replaces the prior one.
    Returns ``{status, chunks, content_hash}``.
    """
    body = (text or "").strip()
    if not body:
        return {"status": "empty", "chunks": 0}
    unit = {
        "anchor": "snapshot",
        "kind": "snapshot",
        "title": _snapshot_title(url, body),
        "heading_path": url or "",
        "level": 0,
        "summary": _snapshot_summary(body),
        "body": body,
        "byte_start": 0,
        "byte_end": len(body.encode()),
    }
    (record,) = index_nonfile(store, embedder, project, "snapshot", url or "", [unit], now=now)
    return {"status": "indexed", "chunks": 1, "content_hash": record["content_hash"]}


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

    store.set_index_meta(project)  # record a human name for the index (viewer dropdown)
    for path in discovered:
        status, n_chunks, source_path = _index_path(store, embedder, distiller, project, project_root, root, path, now)
        if source_path is not None:
            seen.add(source_path)
        if status == "indexed":
            stats["files"] += 1
            stats["chunks"] += n_chunks
        elif status == "skipped":
            stats["skipped"] += 1

    # Drift reconciliation: mark-and-sweep. Only delete sources that were actually scanned.
    # When root ⊊ project_root (a scoped subtree index), `seen` only covers files under root,
    # but indexed_sources covers the whole project. Deleting the difference would wipe everything
    # outside the scoped scope (the environment/ regression: scoped index pruned 2990 chunks).
    # Invariant: mark set (seen) and sweep set must cover the same universe. When scoped, only
    # sweep files under root; when whole-project, sweep everything (fast-path, byte-identical).
    scan_root = root.resolve()
    project_root_resolved = project_root.resolve()
    whole_project = scan_root == project_root_resolved

    for gone in store.indexed_sources(project["key"]) - seen:
        if not whole_project:
            try:
                in_scope = (project_root / gone).resolve().is_relative_to(scan_root)
            except (OSError, ValueError):
                in_scope = False  # unresolvable path: keep it (safe direction, self-heals on next whole-project index)
            if not in_scope:
                continue
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
        unit["summary"] = summary
    return _records_from_units(store, embedder, project, source_path, units)


def _records_from_units(
    store: Store, embedder: EmbeddingGateway, project: Project, source_path: str, units: list[dict]
) -> list[dict]:
    """Embed chunk units and build their stored records — the embed+quantize step shared by the
    file indexer (_build_chunks) and the non-file indexer (index_nonfile). Each unit carries
    anchor/kind/title/heading_path/level/summary/body/byte_start/byte_end; ``summary`` is used
    as-is (any LLM summary is applied by the caller before this).

    Embeds one unit per call, deliberately: on CPU fastembed's batched path measured *slower*
    than per-text inference for chunk-sized inputs (143 × ~1 kB: 6.1 s single vs 7.3–9.1 s
    batched), and batching the file indexer regressed ``eval_code_index`` from 60 s to 104 s."""
    records = []
    for unit in units:
        vec = embedder.embed_one(_embed_text(unit["title"], unit["heading_path"], unit["summary"], unit["body"]))
        blob, scale = quantize_int8(vec)
        records.append(
            {
                "id": store.chunk_id(project["key"], source_path, unit["anchor"]),
                "kind": unit["kind"],
                "anchor": unit["anchor"],
                "title": unit["title"],
                "heading_path": unit["heading_path"],
                "level": unit["level"],
                "summary": unit["summary"],
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


def index_nonfile(
    store: Store,
    embedder: EmbeddingGateway,
    project: Project,
    kind: str,
    source: str,
    units: list[dict],
    *,
    now: float | None = None,
) -> list[dict]:
    """Index units whose source is not a file on disk (a URL, a conversation session) as ``kind``
    chunks, replacing that source's previous ``kind`` chunks; no drift tracking (see
    ``Store.replace_nonfile_chunks``). Returns the stored records."""
    records = _records_from_units(store, embedder, project, source, units)
    store.replace_nonfile_chunks(project["key"], kind, source, records, now if now is not None else time.time())
    return records


def _llm_summary(distiller, heading_path: str, body: str) -> str:
    """Best-effort one-line LLM summary; falls back silently so indexing never breaks."""
    try:
        fact = distiller.summarize(f"Section: {heading_path}\n\n{body[:2000]}")
        return fact.title[:_SUMMARY_CHARS] if fact and fact.title else ""
    except Exception:
        return ""
