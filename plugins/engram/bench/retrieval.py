"""Rank functions over the real recall paths, and the labelled-query scorer, for the bench.

Two production rankers are measured: ``search`` (the per-prompt injection hook — priority
score) and ``search_fused`` (the on-demand ``recall`` tool — weighted rank fusion). Both rank
the full candidate set (``k=10``, similarity gate off) so Recall@k/MRR see every fact.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable

from core.ports.embedding import EmbeddingGateway
from core.recall import search, search_fused
from core.store import Store

RankFn = Callable[[str], list[str]]


def search_ranker(store: Store, embedder: EmbeddingGateway, project: dict, cfg) -> RankFn:
    """Query -> fact texts, best first, via the hook's priority-score path."""

    def rank_fn(query: str) -> list[str]:
        return [row["text"] for _score, row in search(store, embedder, project, query, cfg, k=10, min_sim=-1.0)]

    return rank_fn


def fused_ranker(store: Store, embedder: EmbeddingGateway, project: dict, cfg) -> RankFn:
    """Query -> fact texts, best first, via the ``recall`` tool's rank-fusion path."""

    def rank_fn(query: str) -> list[str]:
        return [row["text"] for _s, _sim, row in search_fused(store, embedder, project, query, cfg, k=10, min_sim=-1.0)]

    return rank_fn


def score_queries(
    queries: list[dict], facts: list[str], rank_fn: RankFn
) -> tuple[float, float, float, float, list[dict]]:
    """Aggregate Recall@1/@3, MRR@10 and mean query latency, plus per-query records.

    The per-query records let conditions evaluated on the same queries be compared with
    paired tests (McNemar, bootstrap) rather than only independent intervals.
    """
    hit1 = hit3 = mrr = 0.0
    latencies = []
    per_query: list[dict] = []
    for item in queries:
        gold = {facts[i] for i in item["relevant"]}
        start = time.perf_counter()
        ranked = rank_fn(item["q"])
        latencies.append((time.perf_counter() - start) * 1000)
        q_hit1 = bool(ranked[:1] and ranked[0] in gold)
        q_hit3 = any(text in gold for text in ranked[:3])
        q_rr = 0.0
        for rank, text in enumerate(ranked[:10], start=1):
            if text in gold:
                q_rr = 1.0 / rank
                break
        hit1 += q_hit1
        hit3 += q_hit3
        mrr += q_rr
        per_query.append({"q": item["q"], "hit1": q_hit1, "hit3": q_hit3, "rr": q_rr})
    n = len(queries)
    return hit1 / n, hit3 / n, mrr / n, statistics.mean(latencies), per_query
