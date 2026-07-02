"""Read side — embed a query, hybrid re-rank active facts, render an injection block.

Ranking is not similarity alone. Each candidate that clears the similarity gate
(the context cue) gets a Priority Score combining similarity, recency decay and
frequency (see ``core.scoring``). Superseded facts are excluded at the SQL layer,
so a replaced fact can never resurface. Everything the model sees is capped by
``max_chars`` so the token budget is bounded.
"""

from __future__ import annotations

import sqlite3
import time

from core.config import Config
from core.embedding import EmbeddingGateway
from core.project import Project
from core.quantize import cosine, dequantize_int8
from core.scoring import frequency_boost, priority, recency_decay
from core.store import Store

Hit = tuple[float, sqlite3.Row]


def _row_vec(row: sqlite3.Row) -> list[float]:
    return dequantize_int8(row["vec_int8"], row["scale"])


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
    scored = _score(store.active_rows_for_project(project["key"]), query_vec, cfg, now, min_sim, 1.0)
    if cross and len(scored) < k:
        others = [r for r in store.active_rows() if r["project_key"] != project["key"]]
        scored += _score(others, query_vec, cfg, now, min_sim, 0.9)
    scored.sort(key=lambda hit: hit[0], reverse=True)
    return scored[:k]


def render_block(header: str, hits: list[Hit], max_chars: int) -> str:
    if not hits:
        return ""
    lines = [header]
    used = len(header)
    for _score_value, row in hits:
        line = f"- {row['text']}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) if len(lines) > 1 else ""
