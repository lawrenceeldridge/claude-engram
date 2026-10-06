"""Cognitive-inspired ranking primitives (pure functions — Functional Core).

Maps three ideas from memory research onto retrieval:
  - Forgetting curve  -> exponential recency decay (reinforcement refreshes it)
  - Consolidation     -> frequency boost (facts seen across sessions strengthen)
  - Context-dependent -> semantic similarity is the retrieval cue (applied upstream)

The final Priority Score orders *non-conflicting* candidates. Genuine conflicts
are handled separately by hard supersession in the store, not by this score — a
stale-but-frequent fact must never out-rank the fact that replaced it.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from operator import itemgetter


def recency_decay(age_seconds: float, half_life_days: float) -> float:
    """1.0 at age 0, 0.5 at one half-life, → 0 as age grows."""
    if half_life_days <= 0:
        return 1.0
    lam = math.log(2) / (half_life_days * 86400.0)
    return math.exp(-lam * max(0.0, age_seconds))


def frequency_boost(frequency: int, cap: int = 8) -> float:
    """0.0 for a fact seen once; grows logarithmically toward 1.0 at ``cap``."""
    if frequency <= 1:
        return 0.0
    return min(1.0, math.log(frequency) / math.log(cap))


def priority(
    similarity: float,
    decay: float,
    freq_boost: float,
    w_sim: float,
    w_recency: float,
    w_freq: float,
) -> float:
    return similarity * w_sim + decay * w_recency + freq_boost * w_freq


def fact_priority(
    row, similarity: float, now: float, half_life_days: float, weights: tuple[float, float, float]
) -> float:
    """A fact row's Priority Score at ``now``: ``priority`` of its similarity, its recency decay
    since last seen (else created), and its frequency boost. ``weights`` = (w_sim, w_recency, w_freq)."""
    seen = row["last_seen"] if row["last_seen"] is not None else row["created_at"]
    decay = recency_decay(now - seen, half_life_days)
    return priority(similarity, decay, frequency_boost(row["frequency"] or 1), *weights)


def top_by_priority(
    rows: Sequence,
    sims: Sequence[float],
    k: int,
    *,
    min_sim: float,
    now: float,
    half_life_days: float,
    weights: tuple[float, float, float],
) -> list[tuple[float, object]]:
    """The ``k`` best ``(fact_priority, row)`` among rows whose similarity clears ``min_sim`` —
    exactly ``sorted(gated, key=score, reverse=True)[:k]`` over rows in their given order —
    without scoring every row.

    Rows are visited in descending similarity. Decay and boost are each at most 1, so with
    non-negative weights no row scores above ``sim·w_sim + w_recency + w_freq``; once that bound
    falls strictly below the k-th best score found, no later row can enter (or tie into) the top
    k. Float rounding is monotone, so the bound holds for the computed scores too. Ties keep row
    order, as the full stable sort does.
    """
    if min(weights) < 0:
        raise ValueError("top_by_priority needs non-negative weights (its bound assumes them)")
    if k <= 0:
        return []
    w_sim, w_recency, w_freq = weights
    best: list[float] = []  # min-heap of the k best scores so far
    found: list[tuple[int, float]] = []
    for i in sorted(range(len(rows)), key=sims.__getitem__, reverse=True):
        sim = sims[i]
        if sim < min_sim or (len(best) == k and sim * w_sim + w_recency + w_freq < best[0]):
            break
        score = fact_priority(rows[i], sim, now, half_life_days, weights)
        found.append((i, score))
        if len(best) < k:
            heapq.heappush(best, score)
        elif score > best[0]:
            heapq.heapreplace(best, score)
    found.sort(key=itemgetter(0))  # back to row order, so the stable sort breaks ties as the full one does
    found.sort(key=itemgetter(1), reverse=True)
    return [(score, rows[i]) for i, score in found[:k]]


# Salience (importance) by observation type — the "how strongly encoded" signal (emotional /
# dopamine tagging in memory research: strong events are stored faster and last longer).
# Deliberate choices and hard-won fixes are the most worth keeping; a passing discovery the
# least. Feeds the *retention* score (the sleep-pass forgetting curve) so important facts fade
# slower — it deliberately does NOT touch the recall Priority Score above, so recall ordering is
# unchanged. Unknown / empty types fall to the neutral default.
_SALIENCE_BY_TYPE = {
    "decision": 1.0,
    "bugfix": 1.0,
    "antipattern": 1.0,
    "change": 0.7,
    "feature": 0.7,
    "refactor": 0.6,
    "session_summary": 0.5,
    "prompt": 0.4,
    "discovery": 0.4,
}
_DEFAULT_SALIENCE = 0.4


def salience_of(type_: str) -> float:
    """Salience (importance) of a fact from its observation ``type``, in [0, 1]. Pure."""
    return _SALIENCE_BY_TYPE.get((type_ or "").strip().lower(), _DEFAULT_SALIENCE)
