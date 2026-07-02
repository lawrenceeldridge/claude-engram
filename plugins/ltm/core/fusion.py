"""Weighted Reciprocal Rank fusion (Functional Core — pure).

Ranking by embedding similarity alone is fragile — especially under the
dependency-free hash embedder, whose "similarity" is only lexical overlap. Fusion
merges several independent ranked channels into one order, so a fact that a weak
embedder misses can still surface on keyword overlap, recency or reinforcement.

Adapted from jcodemunch's retrieval/signal_fusion. Each channel yields a ranked
list of ids; the fused score sums ``weight[c] / (k + rank_c(id))`` across the
channels an id appears in (Reciprocal Rank Fusion, smoothing ``k``). Rank-based,
so channels on incompatible score scales combine cleanly with no normalisation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
) -> list[Fused]:
    """Reciprocal-rank-fuse channels into one list, highest fused score first."""
    effective = dict(DEFAULT_WEIGHTS)
    if weights:
        effective.update(weights)

    accum: dict[str, Fused] = {}
    for channel in channels:
        weight = effective.get(channel.name, 1.0)
        for rank_0, fact_id in enumerate(channel.ranked_ids):
            contribution = weight / (smoothing + rank_0 + 1)
            entry = accum.get(fact_id)
            if entry is None:
                entry = accum[fact_id] = Fused(fact_id=fact_id, score=0.0)
            entry.score += contribution
            entry.contributions[channel.name] = contribution

    return sorted(accum.values(), key=lambda f: f.score, reverse=True)
