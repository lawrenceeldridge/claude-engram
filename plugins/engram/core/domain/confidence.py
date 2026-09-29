"""Recall confidence (Functional Core — pure).

One 0-1 number telling an on-demand caller (the MCP ``recall`` tool) how likely the returned facts
are to hold the answer, so it can trust memory or widen to a full search.

The signal is ``pool_z``: how far the best match stands out from the similarity of **every fact
recall scanned** (a z-score against the pool). Measured on ``engram eval --confidence`` it is the
only candidate that beats the previous gap × strength × identity formula at every store density
(AUROC +0.17 / +0.08 / +0.12 at 0 / 1,788 / 20,000 extra facts), and its Platt parameters barely
move between embedding models. A Platt fit (``sigmoid(a·z + b)``) maps it onto 0-1.

It is a **ranked score, not a probability**: no fixed calibration survives store growth (a fit
that claims 0.75 at one density delivers ~0.55 at another), so read higher as "more likely", and
compare it only against ``recall_min_confidence``. A backend that cannot judge (the lexical
``hash`` stub, whose best candidate is near chance) gets no calibration at all — see
``core.recall.get_calibration``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class PoolStats:
    """Distribution of query similarity over every comparable fact recall scanned.

    The background a top hit is judged against: how similar the *whole store* is to
    the query, not just the runner-up. ``std`` is the population standard deviation.
    """

    n: int
    mean: float
    std: float


@dataclass(frozen=True)
class Calibration:
    """Platt parameters mapping a ``pool_z`` score onto 0-1: ``sigmoid(a * z + b)``."""

    a: float
    b: float


def pool_stats(sims: Iterable[float]) -> PoolStats:
    """One pass over the scanned similarities (callers pass comparable values only)."""
    values = list(sims)
    n = len(values)
    if n == 0:
        return PoolStats(0, 0.0, 0.0)
    mean = math.fsum(values) / n
    variance = max(0.0, math.fsum(v * v for v in values) / n - mean * mean)
    return PoolStats(n, mean, math.sqrt(variance))


def pool_z(sim: float, pool: PoolStats) -> float:
    """How many pool standard deviations ``sim`` sits above the pool mean (0 when the pool is flat)."""
    return (sim - pool.mean) / pool.std if pool.std > 0 else 0.0


def sigmoid(x: float) -> float:
    """Numerically stable logistic function."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def calibrate(score: float, calibration: Calibration) -> float:
    """Platt-scaled 0-1 value of a raw score."""
    return sigmoid(calibration.a * score + calibration.b)


def calibrated_confidence(best_sim: float, pool: PoolStats, calibration: Calibration) -> float:
    """The recall confidence: the best match's ``pool_z``, Platt-scaled, to 3 decimals."""
    return round(calibrate(pool_z(best_sim, pool), calibration), 3)
