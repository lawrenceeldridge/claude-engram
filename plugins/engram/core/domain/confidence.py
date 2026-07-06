"""Calibrated recall confidence (Functional Core — pure).

A single 0-1 score summarising how trustworthy a ranked recall is, so a caller
(the MCP recall tool, a hook) can decide whether to trust memory or widen to a
full search. It is a weighted geometric mean of independent 0-1 signals, so any
single weak signal drags the number down
(the whole point — a lone strong hit with no runner-up gap shouldn't read as
certain).

Signals:
  * gap      — how far the top hit beats the second (ties → low).
  * strength — absolute score of the top hit, soft-squashed to 0-1.
  * identity — 1.0 when the top hit shares a content token with the query
               (an exact lexical anchor), 0.7 when unknown, 0.6 on a known miss.

Freshness is intentionally omitted: recency already lives inside the priority
score these values come from, so folding it in again would double-count it.
"""

from __future__ import annotations

import math

_WEIGHTS = {"gap": 0.35, "strength": 0.40, "identity": 0.25}

# Squash constant for `strength`: 1 - e^(-top1/k). With priority scores in the
# ~0-1.5 range a k of 0.5 saturates a genuinely strong hit while keeping a
# barely-over-threshold hit low.
_STRENGTH_K = 0.5


def compute_confidence(
    scores: list[float],
    *,
    has_identity_match: bool | None = None,
) -> dict:
    """Return ``{"confidence": float, "components": {...}}`` for a ranked score list.

    ``scores`` must be sorted descending (as ``recall.search`` returns them).
    Components are returned alongside so a debug caller can see *why* a number
    was low.
    """
    components = {
        "gap": 0.0,
        "strength": 0.0,
        "identity": 1.0 if has_identity_match else (0.7 if has_identity_match is None else 0.6),
    }
    if not scores:
        return {"confidence": 0.0, "components": components}

    top1 = scores[0]
    top2 = scores[1] if len(scores) > 1 else 0.0

    components["gap"] = 0.0 if top1 <= 0 else max(0.0, min(1.0, (top1 - top2) / top1))
    components["strength"] = max(0.0, min(1.0, 1.0 - math.exp(-top1 / _STRENGTH_K)))

    return {"confidence": _combine(components), "components": components}


def _combine(components: dict) -> float:
    log_sum = 0.0
    for key, weight in _WEIGHTS.items():
        value = max(1e-6, float(components.get(key, 0.0)))
        log_sum += weight * math.log(value)
    return round(math.exp(log_sum), 3)
