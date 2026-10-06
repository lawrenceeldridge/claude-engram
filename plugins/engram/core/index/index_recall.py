"""Read side of the code/docs index — hybrid search, exact-span fetch, outlines.

Search fuses two channels the store already provides — FTS5 ``bm25()`` keyword
ranking (column-weighted) and fastembed cosine similarity — with the same Reciprocal
Rank Fusion used for fact recall, then greedy-packs the winners under a character
budget with a per-file diversity cap (score decays ``0.5^n`` per repeat from one
file) so a result set is never flooded with near-adjacent sections of one document.

A search given a ``TimeWindow`` (``search_history``'s ``after`` / ``before``) boosts each fused
candidate by its proximity to the window (``core.domain.temporal``). It re-orders the candidates
the query already retrieved; it never admits a chunk the query didn't match.

Search returns outline rows (anchor/title/summary/freshness), never bodies — the
token-saving move. A follow-up ``get_chunk`` fetches one section's body and verifies
it section-precisely against the live file. Freshness is file-level in search (one
``stat`` per distinct source) and section-level in ``get_chunk``.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from core.config import Config
from core.domain.fusion import Channel, fuse
from core.domain.temporal import TimeWindow, boost_by_window
from core.ports.embedding import EmbeddingGateway
from core.ports.scorer import DIM_MISMATCH, get_scorer
from core.project import Project
from core.store import Store

_PER_FILE_CAP = 3
_FRESH_WEIGHTS = {"similarity": 1.0, "fts": 0.8}


def _cosine_ranked(cfg: Config, embedder: EmbeddingGateway, rows, query: str, k: int) -> list[str]:
    """Chunk ids ranked by cosine to the query — the same ``VectorScorer`` recall scans facts with
    (vectorised where numpy is present) — skipping unembedded rows and other-dimension vectors."""
    embedded = [row for row in rows if row["vec_int8"]]
    sims = get_scorer(cfg).cosine_all(embedded, embedder.embed_query(query))
    scored = [(sim, row["id"]) for sim, row in zip(sims, embedded) if sim != DIM_MISMATCH]
    scored.sort(reverse=True)
    return [cid for _sim, cid in scored[:k]]


def _file_freshness(project_root: str, source_path: str, stored_hash: str) -> str:
    """Cheap file-level freshness for search hits: fresh | edited | gone."""
    try:
        data = (Path(project_root) / source_path).read_bytes()
    except OSError:
        return "gone"
    return "fresh" if hashlib.sha256(data).hexdigest() == stored_hash else "edited"


# A snapshot chunk's source is a URL, not a file on disk, so freshness is age-based, not drift:
# a page capture simply ages out (the live page may have moved on) — it is never "edited"/"gone".
_SNAPSHOT_FRESH_SECONDS = 86400.0  # captures under a day old read as fresh; older as stale


def _snapshot_freshness(indexed_at: float | None, now: float) -> str:
    """Age-based freshness for a snapshot chunk: fresh | stale (never file-drift)."""
    return "fresh" if (now - (indexed_at or 0.0)) <= _SNAPSHOT_FRESH_SECONDS else "stale"


def _conversation_freshness(indexed_at: float | None, now: float) -> str:
    """An exchange records a conversation that already happened — it never drifts."""
    return "fresh"


# Chunk kinds whose source is not a file on disk: freshness is decided by kind, never file drift.
_NONFILE_FRESHNESS = {"snapshot": _snapshot_freshness, "exchange": _conversation_freshness}
NONFILE_KINDS = frozenset(_NONFILE_FRESHNESS)


def _nonfile_freshness(row, now: float) -> str | None:
    """Freshness for a non-file chunk (snapshot / exchange), or None for a file-backed chunk."""
    rule = _NONFILE_FRESHNESS.get(row["kind"])
    return rule(row["indexed_at"], now) if rule else None


def _diverse_pack(fused, rows: dict, max_chars: int, *, diverse: bool = True) -> list[tuple]:
    """Greedy budget pack with a per-file cap + ``0.5^n`` same-file score decay. ``diverse=False``
    (a search already scoped to one source) packs in fused order with no per-source cap."""
    per_file: dict[str, int] = {}
    scored: list[tuple] = []
    for entry in fused:
        row = rows.get(entry.fact_id)
        if row is None:
            continue
        seen = per_file.get(row["source_path"], 0) if diverse else 0
        if seen >= _PER_FILE_CAP:
            continue
        per_file[row["source_path"]] = seen + 1
        scored.append((row, entry.score * (0.5**seen)))
    scored.sort(key=lambda rs: rs[1], reverse=True)

    packed: list[tuple] = []
    used = 0
    for row, score in scored:
        text = row["summary"] or row["title"] or ""
        if packed and used + len(text) > max_chars:
            break
        packed.append((row, score))
        used += len(text)
    return packed


def search_index(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    query: str,
    *,
    k: int | None = None,
    max_chars: int | None = None,
    kind: str | None = None,
    source_path: str | None = None,
    window: TimeWindow | None = None,
) -> dict:
    """Hybrid keyword+semantic search over indexed chunks. Returns outline rows only.

    ``kind`` scopes the search to one chunk kind (``doc_section`` / ``code_symbol`` / ``snapshot`` /
    ``exchange``); None searches the whole index. ``source_path`` scopes it to one source (a file,
    a snapshot URL, or an episode), which also lifts the per-source diversity cap. ``window`` boosts
    chunks indexed inside or near it (soft — see the module docstring); None leaves ranking as is.
    """
    k = k or 10
    max_chars = cfg.recall_max_chars if max_chars is None else max_chars
    query = (query or "").strip()
    if not query:
        return {"query": query, "project": project["label"], "results": [], "returned": 0, "matched": 0}

    pool = max(k * 4, 40)
    rows = {r["id"]: r for r in store.chunk_rows(project["key"], kind=kind, source_path=source_path)}
    fts_ids = store.chunk_fts_search(project["key"], query, limit=pool, kind=kind, source_path=source_path)
    cos_ids = _cosine_ranked(cfg, embedder, rows.values(), query, k=pool)
    fused = fuse(
        [Channel("fts", fts_ids), Channel("similarity", cos_ids)],
        weights=_FRESH_WEIGHTS,
    )
    if window is not None:
        stamps = {f.fact_id: rows[f.fact_id]["indexed_at"] for f in fused if f.fact_id in rows}
        fused = boost_by_window(fused, stamps, window)
    packed = _diverse_pack(fused, rows, max_chars, diverse=source_path is None)[:k]

    now = time.time()
    fresh_cache: dict[str, str] = {}
    results = []
    for row, score in packed:
        sp = row["source_path"]
        fresh = _nonfile_freshness(row, now)
        if fresh is None:
            if sp not in fresh_cache:
                fresh_cache[sp] = _file_freshness(project["path"], sp, _source_hash(store, project["key"], sp))
            fresh = fresh_cache[sp]
        results.append(
            {
                "anchor": row["anchor"],
                "title": row["title"],
                "kind": row["kind"],
                "heading_path": row["heading_path"],
                "source_path": sp,
                "summary": row["summary"] or "",
                "score": round(float(score), 4),
                "freshness": fresh,
            }
        )
    return {
        "query": query,
        "project": project["label"],
        "results": results,
        "returned": len(results),
        "matched": len(fused),
    }


def _source_hash(store: Store, project_key: str, source_path: str) -> str:
    state = store.source_state(project_key, source_path)
    return state[0] if state else ""


def get_chunk(store: Store, project: Project, ref: str) -> dict:
    """Fetch one chunk's full body. A file-backed chunk is verified section-precisely against the
    live file; a non-file chunk (snapshot / exchange) takes its kind's freshness rule."""
    row = store.get_chunk(project["key"], ref)
    if row is None:
        return {"found": False, "ref": ref}
    return {
        "found": True,
        "anchor": row["anchor"],
        "title": row["title"],
        "kind": row["kind"],
        "heading_path": row["heading_path"],
        "source_path": row["source_path"],
        "body": row["body"],
        "freshness": _nonfile_freshness(row, time.time()) or _section_freshness(project["path"], row),
    }


def _section_freshness(project_root: str, row) -> str:
    """Anchor-precise freshness: re-parse the live file and compare this unit's body hash."""
    path = Path(project_root) / row["source_path"]
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "gone"
    for anchor, body in _live_units(row["kind"], text, path):
        if anchor == row["anchor"]:
            live = hashlib.sha256(body.encode()).hexdigest()
            return "fresh" if live == row["content_hash"] else "edited"
    return "stale"  # the heading/symbol this anchor named no longer exists


def _live_units(kind: str, text: str, path: Path):
    """(anchor, body) pairs from the live file, parsed and disambiguated as the indexer did."""
    if kind == "code_symbol":
        from core.index.code_symbols import extract_code_symbols

        raw = [(s.qualname, s.body) for s in extract_code_symbols(text, path.suffix)]
        return _dedupe_anchors(raw)  # match the indexer's overload disambiguation
    from core.index.chunking import split_markdown

    return [(s.slug, s.body) for s in split_markdown(text, path.stem)]


def _dedupe_anchors(units: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Suffix duplicate anchors ~2, ~3 … so freshness lookups match stored chunk anchors."""
    seen: dict[str, int] = {}
    out = []
    for anchor, body in units:
        if anchor in seen:
            seen[anchor] += 1
            anchor = f"{anchor}~{seen[anchor]}"
        else:
            seen[anchor] = 1
        out.append((anchor, body))
    return out


def get_outline(store: Store, project: Project, source_path: str | None = None, kind: str | None = None) -> dict:
    """Repo/file skeleton — anchors, titles, breadcrumbs, summaries; zero bodies."""
    rows = store.chunk_outline(project["key"], source_path, kind=kind)
    return {
        "project": project["label"],
        "source_path": source_path,
        "sections": [
            {
                "anchor": r["anchor"],
                "title": r["title"],
                "kind": r["kind"],
                "heading_path": r["heading_path"],
                "level": r["level"],
                "source_path": r["source_path"],
                "summary": r["summary"] or "",
            }
            for r in rows
        ],
        "count": len(rows),
    }
