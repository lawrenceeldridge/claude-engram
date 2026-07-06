"""Read side — embed a query, hybrid re-rank active facts, render an injection block.

Ranking is not similarity alone. Each candidate that clears the similarity gate
(the context cue) gets a Priority Score combining similarity, recency decay and
frequency (see ``core.domain.scoring``). Superseded facts are excluded at the SQL layer,
so a replaced fact can never resurface. Everything the model sees is capped by
``max_chars`` so the token budget is bounded.
"""

from __future__ import annotations

import sqlite3
import time

from core.config import Config
from core.domain.fusion import Channel, fuse
from core.domain.lexical import token_set
from core.domain.quantize import cosine, dequantize_int8
from core.domain.scoring import frequency_boost, priority, recency_decay
from core.ports.embedding import EmbeddingGateway
from core.project import GLOBAL_PROJECT_KEY, Project
from core.store import Store

Hit = tuple[float, sqlite3.Row]
# (fused_score, cosine_similarity, row) — fusion decides order; similarity feeds confidence.
FusedHit = tuple[float, float, sqlite3.Row]


def _row_vec(row: sqlite3.Row) -> list[float]:
    return dequantize_int8(row["vec_int8"], row["scale"])


def _recall_rows(store: Store, project_key: str) -> list[sqlite3.Row]:
    """Active facts for the project, unioned with globally-scoped anti-patterns.

    A narrow, kind-only exception to project scoping: a tool/harness lesson applies in every
    project. Adds nothing (and costs nothing) when no global anti-patterns exist — the common
    case, and always true in the eval store, so recall is unchanged there.
    """
    rows = store.active_rows_for_project(project_key)
    if project_key != GLOBAL_PROJECT_KEY:
        rows = rows + store.active_antipatterns(GLOBAL_PROJECT_KEY)
    return rows


def _score(rows, query_vec, cfg: Config, now: float, min_sim: float, penalty: float):
    out = []
    qdim = len(query_vec)
    for row in rows:
        if row["dim"] and row["dim"] != qdim:
            continue  # different embedder — vectors aren't comparable
        sim = cosine(query_vec, _row_vec(row))
        if sim < min_sim:
            continue
        age = now - (row["last_seen"] if row["last_seen"] is not None else row["created_at"])
        decay = recency_decay(age, cfg.half_life_days)
        boost = frequency_boost(row["frequency"] or 1)
        score = priority(sim, decay, boost, cfg.w_sim, cfg.w_recency, cfg.w_freq) * penalty
        # Short-term facts can be down-weighted at recall (context-dependent retrieval).
        # Default weight 1.0 is a no-op — and `tier` is only read when a penalty is set,
        # so behaviour (and old rows without the column) is untouched by default.
        if cfg.stm_recall_weight != 1.0 and row["tier"] == "stm":
            score *= cfg.stm_recall_weight
        out.append((score, row))
    return out


def search(
    store: Store,
    embedder: EmbeddingGateway,
    project: Project,
    query: str,
    cfg: Config,
    *,
    k: int | None = None,
    min_sim: float | None = None,
    cross_project: bool | None = None,
    now: float | None = None,
) -> list[Hit]:
    k = cfg.top_k if k is None else k
    min_sim = cfg.min_sim if min_sim is None else min_sim
    cross = cfg.cross_project if cross_project is None else cross_project
    now = now if now is not None else time.time()

    query_vec = embedder.embed_query(query)
    scored = _score(_recall_rows(store, project["key"]), query_vec, cfg, now, min_sim, 1.0)
    if cross and len(scored) < k:
        others = [r for r in store.active_rows() if r["project_key"] != project["key"]]
        scored += _score(others, query_vec, cfg, now, min_sim, 0.9)
    scored.sort(key=lambda hit: hit[0], reverse=True)
    return scored[:k]


def search_fused(
    store: Store,
    embedder: EmbeddingGateway,
    project: Project,
    query: str,
    cfg: Config,
    *,
    k: int | None = None,
    min_sim: float | None = None,
) -> list[FusedHit]:
    """Rank facts by Weighted Reciprocal Rank fusion of four channels.

    A candidate qualifies if it clears the similarity floor, shares a content
    token with the query, *or* is an FTS keyword hit — so lexical signal rescues
    facts a weak embedder ranks below the gate (including matches on the title /
    narrative fields, which are not embedded). Returns ``(fused_score,
    cosine_similarity, row)`` in fused order; the similarity is carried through so
    confidence stays interpretable regardless of fused-score magnitude.
    """
    k = cfg.top_k if k is None else k
    min_sim = cfg.min_sim if min_sim is None else min_sim
    query_vec = embedder.embed_query(query)
    qdim = len(query_vec)
    query_tokens = token_set(query)

    rows_by_id: dict[str, sqlite3.Row] = {}
    candidates: dict[str, tuple[float, int, sqlite3.Row]] = {}
    for row in _recall_rows(store, project["key"]):
        if row["dim"] and row["dim"] != qdim:
            continue
        rows_by_id[row["id"]] = row
        sim = cosine(query_vec, _row_vec(row))
        overlap = len(query_tokens & token_set(row["text"]))
        if sim < min_sim and overlap == 0:
            continue
        candidates[row["id"]] = (sim, overlap, row)

    fts_ids = store.fts_search(project["key"], query, limit=max(k * 4, 50))
    if project["key"] != GLOBAL_PROJECT_KEY:  # global anti-patterns are first-class in the FTS channel too
        fts_ids = fts_ids + store.fts_search(GLOBAL_PROJECT_KEY, query, limit=max(k * 4, 50))
    for fid in fts_ids:
        if fid not in candidates and fid in rows_by_id:
            row = rows_by_id[fid]
            candidates[fid] = (cosine(query_vec, _row_vec(row)), 0, row)

    if not candidates:
        return []

    fts_rank = {fid: i for i, fid in enumerate(fid for fid in fts_ids if fid in candidates)}

    def ranked_by(key, predicate=lambda v: True) -> list[str]:
        items = [(fid, key(v)) for fid, v in candidates.items() if predicate(v)]
        items.sort(key=lambda x: x[1], reverse=True)
        return [fid for fid, _ in items]

    channels = [
        Channel("similarity", ranked_by(lambda v: v[0], lambda v: v[0] > 0)),
        Channel("lexical", ranked_by(lambda v: v[1], lambda v: v[1] > 0)),
        Channel("fts", sorted(fts_rank, key=fts_rank.get)),
        Channel("recency", ranked_by(lambda v: v[2]["last_seen"] or v[2]["created_at"])),
        Channel("frequency", ranked_by(lambda v: v[2]["frequency"] or 1)),
    ]

    fused = fuse(channels)
    out: list[FusedHit] = []
    for entry in fused[:k]:
        sim, _overlap, row = candidates[entry.fact_id]
        out.append((entry.score, sim, row))
    return out


def render_block(header: str, hits: list[Hit], max_chars: int) -> tuple[str, list[str]]:
    """Render the injected DTO (one line per fact) and return it with the ids of the
    facts actually included, so the caller can attribute retrieval (recall_count).

    The ids are the single source of truth for "what was put in front of the model":
    only rows that fit under ``max_chars`` are counted. Returns ``("", [])`` on empty
    or all-truncated input — the Null Object (inject nothing, attribute nothing).
    """
    if not hits:
        return "", []
    lines = [header]
    ids: list[str] = []
    used = len(header)
    for _score_value, row in hits:
        line = f"- {row['text']}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        ids.append(row["id"])
        used += len(line) + 1
    if len(lines) == 1:
        return "", []
    return "\n".join(lines), ids
