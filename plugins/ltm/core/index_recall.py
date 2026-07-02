"""Read side of the code/docs index — hybrid search, exact-span fetch, outlines.

Search fuses two channels the store already provides — FTS5 ``bm25()`` keyword
ranking (column-weighted) and fastembed cosine similarity — with the same Reciprocal
Rank Fusion used for fact recall, then greedy-packs the winners under a character
budget with a per-file diversity cap (score decays ``0.5^n`` per repeat from one
file) so a result set is never flooded with near-adjacent sections of one document.

Search returns outline rows (anchor/title/summary/freshness), never bodies — the
token-saving move. A follow-up ``get_chunk`` fetches one section's body and verifies
it section-precisely against the live file. Freshness is file-level in search (one
``stat`` per distinct source) and section-level in ``get_chunk``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.config import Config
from core.embedding import EmbeddingGateway
from core.fusion import Channel, fuse
from core.project import Project
from core.quantize import cosine, dequantize_int8
from core.store import Store

_PER_FILE_CAP = 3
_FRESH_WEIGHTS = {"similarity": 1.0, "fts": 0.8}


def _cosine_ranked(store: Store, embedder: EmbeddingGateway, project_key: str, query: str, k: int) -> list[str]:
    """Chunk ids ranked by cosine to the query, dropping vectors of a different dimension."""
    qvec = embedder.embed_one(query)
    qdim = len(qvec)
    scored: list[tuple[float, str]] = []
    for row in store.chunk_rows(project_key):
        if not row["vec_int8"] or (row["dim"] and row["dim"] != qdim):
            continue
        sim = cosine(qvec, dequantize_int8(row["vec_int8"], row["scale"]))
        scored.append((sim, row["id"]))
    scored.sort(reverse=True)
    return [cid for _sim, cid in scored[:k]]


def _file_freshness(project_root: str, source_path: str, stored_hash: str) -> str:
    """Cheap file-level freshness for search hits: fresh | edited | gone."""
    try:
        data = (Path(project_root) / source_path).read_bytes()
    except OSError:
        return "gone"
    return "fresh" if hashlib.sha256(data).hexdigest() == stored_hash else "edited"


def _diverse_pack(fused, rows: dict, max_chars: int) -> list[tuple]:
    """Greedy budget pack with a per-file cap + ``0.5^n`` same-file score decay."""
    per_file: dict[str, int] = {}
    scored: list[tuple] = []
    for entry in fused:
        row = rows.get(entry.fact_id)
        if row is None:
            continue
        seen = per_file.get(row["source_path"], 0)
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
) -> dict:
    """Hybrid keyword+semantic search over indexed doc sections. Returns outline rows only."""
    k = k or 10
    max_chars = cfg.recall_max_chars if max_chars is None else max_chars
    query = (query or "").strip()
    if not query:
        return {"query": query, "project": project["label"], "results": [], "returned": 0, "matched": 0}

    pool = max(k * 4, 40)
    fts_ids = store.chunk_fts_search(project["key"], query, limit=pool)
    cos_ids = _cosine_ranked(store, embedder, project["key"], query, k=pool)
    fused = fuse(
        [Channel("fts", fts_ids), Channel("similarity", cos_ids)],
        weights=_FRESH_WEIGHTS,
    )
    rows = {r["id"]: r for r in store.chunk_rows(project["key"])}
    packed = _diverse_pack(fused, rows, max_chars)[:k]

    fresh_cache: dict[str, str] = {}
    results = []
    for row, score in packed:
        sp = row["source_path"]
        if sp not in fresh_cache:
            fresh_cache[sp] = _file_freshness(project["path"], sp, _source_hash(store, project["key"], sp))
        results.append(
            {
                "anchor": row["anchor"],
                "title": row["title"],
                "heading_path": row["heading_path"],
                "source_path": sp,
                "summary": row["summary"] or "",
                "score": round(float(score), 4),
                "freshness": fresh_cache[sp],
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
    """Fetch one section's full body and verify it section-precisely against the live file."""
    row = store.get_chunk(project["key"], ref)
    if row is None:
        return {"found": False, "ref": ref}
    return {
        "found": True,
        "anchor": row["anchor"],
        "title": row["title"],
        "heading_path": row["heading_path"],
        "source_path": row["source_path"],
        "body": row["body"],
        "freshness": _section_freshness(project["path"], row),
    }


def _section_freshness(project_root: str, row) -> str:
    """Section-precise freshness: re-split the live file and compare this anchor's hash."""
    from core.chunking import split_markdown

    path = Path(project_root) / row["source_path"]
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "gone"
    for section in split_markdown(text, path.stem):
        if section.slug == row["anchor"]:
            live = hashlib.sha256(section.body.encode()).hexdigest()
            return "fresh" if live == row["content_hash"] else "edited"
    return "stale"  # the heading this anchor named no longer exists


def get_outline(store: Store, project: Project, source_path: str | None = None) -> dict:
    """Repo/file skeleton — anchors, titles, breadcrumbs, summaries; zero bodies."""
    rows = store.chunk_outline(project["key"], source_path)
    return {
        "project": project["label"],
        "source_path": source_path,
        "sections": [
            {
                "anchor": r["anchor"],
                "title": r["title"],
                "heading_path": r["heading_path"],
                "level": r["level"],
                "source_path": r["source_path"],
                "summary": r["summary"] or "",
            }
            for r in rows
        ],
        "count": len(rows),
    }
