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
from dataclasses import dataclass

from core.config import Config
from core.domain.confidence import Calibration, PoolStats, calibrated_confidence, pool_stats
from core.domain.fusion import Channel, fuse
from core.domain.lexical import token_set
from core.domain.scoring import frequency_boost, priority, recency_decay
from core.domain.spreading import spread
from core.ports.embedding import EmbeddingGateway
from core.ports.scorer import DIM_MISMATCH, VectorScorer, get_scorer
from core.project import GLOBAL_PROJECT_KEY, Project
from core.store import Store

Hit = tuple[float, sqlite3.Row]
# (fused_score, cosine_similarity, row) — fusion decides order; similarity feeds confidence.
FusedHit = tuple[float, float, sqlite3.Row]


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


def _score(rows, query_vec, cfg: Config, now: float, min_sim: float, penalty: float, scorer: VectorScorer):
    out = []
    sims = scorer.cosine_all(rows, query_vec)
    for row, sim in zip(rows, sims):
        # DIM_MISMATCH (-inf, incomparable embedder dim) or below the gate — both skip.
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

    scorer = get_scorer(cfg)
    query_vec = embedder.embed_query(query)
    scored = _score(_recall_rows(store, project["key"]), query_vec, cfg, now, min_sim, 1.0, scorer)
    if cross and len(scored) < k:
        others = [r for r in store.active_rows() if r["project_key"] != project["key"]]
        scored += _score(others, query_vec, cfg, now, min_sim, 0.9, scorer)
    # Spreading activation (Idea #4): boost candidates co-activated with other candidates via
    # the association graph. Gated — spread_weight 0 (default) skips it entirely, so the hot
    # path (and its cost) is byte-identical when off. The boost math is pure; the shell loads
    # a bounded neighbour set.
    if cfg.spread_weight > 0 and scored:
        ids = [row["id"] for _s, row in scored]
        boosts = spread(ids, store.neighbours(ids), cfg.spread_weight)
        if boosts:
            scored = [(s + boosts.get(row["id"], 0.0), row) for s, row in scored]
    scored.sort(key=lambda hit: hit[0], reverse=True)
    return scored[:k]


@dataclass(frozen=True)
class FusedResult:
    """Fused hits plus the similarity distribution of the whole scanned pool."""

    hits: list[FusedHit]
    pool: PoolStats


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
    """Rank facts by Weighted Reciprocal Rank fusion (see ``search_fused_with_stats``)."""
    return search_fused_with_stats(store, embedder, project, query, cfg, k=k, min_sim=min_sim).hits


def search_fused_with_stats(
    store: Store,
    embedder: EmbeddingGateway,
    project: Project,
    query: str,
    cfg: Config,
    *,
    k: int | None = None,
    min_sim: float | None = None,
) -> FusedResult:
    """Rank facts by Weighted Reciprocal Rank fusion of five channels.

    A candidate qualifies if it clears the similarity floor, shares a content
    token with the query, *or* is an FTS keyword hit — so lexical signal rescues
    facts a weak embedder ranks below the gate (including matches on the title /
    narrative fields, which are not embedded). Hits are ``(fused_score,
    cosine_similarity, row)`` in fused order; the similarity is carried through so
    confidence stays interpretable regardless of fused-score magnitude. ``pool``
    summarises the similarity of every comparable fact scanned — the background a
    top hit's strength is judged against.
    """
    k = cfg.top_k if k is None else k
    min_sim = cfg.min_sim if min_sim is None else min_sim
    scorer = get_scorer(cfg)
    query_vec = embedder.embed_query(query)
    query_tokens = token_set(query)

    rows = _recall_rows(store, project["key"])
    sims = scorer.cosine_all(rows, query_vec)
    rows_by_id: dict[str, sqlite3.Row] = {}
    sim_by_id: dict[str, float] = {}
    candidates: dict[str, tuple[float, int, sqlite3.Row]] = {}
    for row, sim in zip(rows, sims):
        if sim == DIM_MISMATCH:
            continue  # different embedder — vectors aren't comparable
        rows_by_id[row["id"]] = row
        sim_by_id[row["id"]] = sim
        overlap = len(query_tokens & token_set(row["text"]))
        if sim < min_sim and overlap == 0:
            continue
        candidates[row["id"]] = (sim, overlap, row)

    fts_ids = store.fts_search(project["key"], query, limit=max(k * 4, 50))
    if project["key"] != GLOBAL_PROJECT_KEY:  # global anti-patterns are first-class in the FTS channel too
        fts_ids = fts_ids + store.fts_search(GLOBAL_PROJECT_KEY, query, limit=max(k * 4, 50))
    for fid in fts_ids:
        if fid not in candidates and fid in rows_by_id:
            candidates[fid] = (sim_by_id[fid], 0, rows_by_id[fid])

    pool = pool_stats(sim_by_id.values())
    if not candidates:
        return FusedResult([], pool)

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
    return FusedResult(out, pool)


def best_match(hits: list[FusedHit]) -> FusedHit | None:
    """The hit with the highest cosine — what confidence judges, even when fusion's
    recency/frequency channels rank it below a newer, weaker hit."""
    return max(hits, key=lambda hit: hit[1]) if hits else None


# Platt fit of ``pool_z`` for semantic embedders at realistic density — the full-sample fit of
# `engram eval --backends fastembed --confidence --distractors 20000 --distractor-project <a large
# off-topic project>` (bge-base, 333 labelled queries, 2026-09-29), re-checked on a held-out half
# (2026-10-05: a dev-only refit puts the `ok` boundary at z 4.49 vs these constants' 4.52; every
# figure within the held-out interval). Fitted where real stores live (tens of thousands of facts):
# on a small store the same score reads lower, so `ok` is rarer there but more often right.
# See ``core.domain.confidence``.
SEMANTIC_CALIBRATION = Calibration(a=0.6341, b=-3.2706)


def get_calibration(embedder: EmbeddingGateway) -> Calibration | None:
    """Plugin selection of the confidence calibration for the embedder actually in use.

    ``None`` for a gateway whose vectors aren't ``semantic`` (the lexical ``hash`` stub) — the
    honest Special Case: its best confidence signal is near chance, so it reports no score and
    never an ``ok`` verdict. Keyed on the gateway's declared capability, not on ``cfg.embedding``
    (``get_embedder`` falls back to the stub when fastembed can't load) nor on a concrete type.
    """
    return SEMANTIC_CALIBRATION if embedder.semantic else None


def recall_confidence(result: FusedResult, calibration: Calibration | None) -> float | None:
    """The recall tool's confidence for a fused result: 0 when nothing came back, ``None`` when
    the backend can't judge, else the best match's calibrated ``pool_z``.

    The single composition of the confidence inputs — shared by ``recall_structured`` and the
    calibration benchmark, so what is measured is exactly what ships.
    """
    best = best_match(result.hits)
    if best is None:
        return 0.0
    if calibration is None:
        return None
    return calibrated_confidence(best[1], result.pool, calibration)


def is_trusted(confidence: float | None, min_confidence: float) -> bool:
    """The ``ok`` rule: a judged confidence at or above the configured threshold."""
    return confidence is not None and confidence >= min_confidence


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


def render_scaffold(header: str, hits: list[Hit], max_chars: int) -> tuple[str, list[str]]:
    """LT-WM retrieval structure (Ericsson & Kintsch): render the session core as a titled
    scaffold — facts grouped under their card ``title`` — rather than a flat list, so the
    stable orientation block reads as an index card of the project's topics.

    Same contract as ``render_block``: one line per fact, ``max_chars``-capped, returns the
    included ids, and the Null Object (``("", [])``) on empty/all-truncated input. Untitled
    facts group under a plain heading so behaviour is preserved when no titles exist.
    """
    if not hits:
        return "", []
    groups: dict[str, list[sqlite3.Row]] = {}
    order: list[str] = []
    for _score_value, row in hits:
        title = (row["title"] or "").strip() if "title" in row.keys() else ""
        key = title or "Notes"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    lines = [header]
    ids: list[str] = []
    used = len(header)
    for key in order:
        heading = f"  {key}:"
        if used + len(heading) + 1 > max_chars:
            break
        pending = [heading]
        pending_len = len(heading) + 1
        rows_added = []
        for row in groups[key]:
            line = f"    - {row['text']}"
            if used + pending_len + len(line) + 1 > max_chars:
                break
            pending.append(line)
            pending_len += len(line) + 1
            rows_added.append(row["id"])
        if rows_added:  # only emit a heading that carries at least one fact
            lines.extend(pending)
            ids.extend(rows_added)
            used += pending_len
    if len(lines) == 1:
        return "", []
    return "\n".join(lines), ids
