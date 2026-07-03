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

import math


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
