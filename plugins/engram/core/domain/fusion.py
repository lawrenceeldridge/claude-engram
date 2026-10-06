"""Weighted Reciprocal Rank fusion (Functional Core — pure).

Ranking by embedding similarity alone is fragile — especially under the
dependency-free hash embedder, whose "similarity" is only lexical overlap. Fusion
merges several independent ranked channels into one order, so a fact that a weak
embedder misses can still surface on keyword overlap, recency or reinforcement.

Each channel yields a ranked list of ids; the fused score sums
``weight[c] / (k + rank_c(id))`` across the
channels an id appears in (Reciprocal Rank Fusion, smoothing ``k``). Rank-based,
so channels on incompatible score scales combine cleanly with no normalisation.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from operator import itemgetter

# Channel weights, tuned for a text-fact store. Similarity carries semantic
# intent; lexical and fts (keyword/BM25, the latter also over title/narrative) are
# weighted high because they rescue the hash embedder's blind spots; recency and
# reinforcement are tie-breakers.
DEFAULT_WEIGHTS = {"similarity": 1.0, "lexical": 0.8, "fts": 0.6, "recency": 0.4, "frequency": 0.3}

DEFAULT_SMOOTHING = 60


@dataclass
class Channel:
    name: str
    ranked_ids: list[str]


@dataclass
class Fused:
    fact_id: str
    score: float
    contributions: dict[str, float] = field(default_factory=dict)


def fuse(
    channels: list[Channel],
    *,
    weights: dict[str, float] | None = None,
    smoothing: int = DEFAULT_SMOOTHING,
    limit: int | None = None,
) -> list[Fused]:
    """Reciprocal-rank-fuse channels into one list, highest fused score first — the first
    ``limit`` of it when given (ties keep first-seen order either way).

    Scores accumulate as plain floats, in channel order, and a ``Fused`` (with its per-channel
    contributions) is built only for what is returned: at 10⁵ candidates the per-candidate
    objects, not the arithmetic, were the cost.
    """
    effective = dict(DEFAULT_WEIGHTS)
    if weights:
        effective.update(weights)
    channel_weights = [(channel, effective.get(channel.name, 1.0)) for channel in channels]

    scores: dict[str, float] = {}
    for channel, weight in channel_weights:
        current = scores.get
        for rank_0, fact_id in enumerate(channel.ranked_ids):
            scores[fact_id] = current(fact_id, 0.0) + weight / (smoothing + rank_0 + 1)

    if limit is None:
        ranked = sorted(scores.items(), key=itemgetter(1), reverse=True)
    else:
        ranked = heapq.nlargest(limit, scores.items(), key=itemgetter(1))  # == sorted(...)[:limit], stably
    out = {fact_id: Fused(fact_id=fact_id, score=score) for fact_id, score in ranked}
    for channel, weight in channel_weights:
        for rank_0, fact_id in enumerate(channel.ranked_ids):
            entry = out.get(fact_id)
            if entry is not None:
                entry.contributions[channel.name] = weight / (smoothing + rank_0 + 1)
    return list(out.values())
