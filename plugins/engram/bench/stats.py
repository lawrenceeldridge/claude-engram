"""Statistics for the bench harness — pure, stdlib, seeded.

Shared by ``run_eval`` (retrieval quality), ``confidence_eval`` (calibration of the
recall verdict) and ``longmemeval``. A bug here becomes a false claim in a design doc, so every
function is pinned to hand-computed values in ``tests/test_bench_stats.py``. Randomness is always
seeded — every run of the bench prints the same numbers.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Callable, Hashable, Sequence
from typing import TypeVar

from core.domain.confidence import sigmoid

T = TypeVar("T")

SPLIT_SALT = "engram-holdout-v1"  # changing it re-deals every split: a new hold-out, not a tweak


def _split_rank(key: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{SPLIT_SALT}\0{key}".encode()).digest()[:8], "big")


def stable_split(
    items: Sequence[T],
    key: Callable[[T], str],
    dev_fraction: float,
    stratum: Callable[[T], Hashable] | None = None,
) -> tuple[list[T], list[T]]:
    """Deal ``items`` into a ``(dev, test)`` hold-out split — the one definition every harness uses.

    Within each stratum the ``round(dev_fraction * n)`` items with the lowest salted hash of their
    ``key`` go to dev, the rest to test, so each stratum keeps its share exactly. Membership depends
    only on the keys — not on input order and not on a seed — and adding an item to a stratum moves
    at most one existing item across its boundary. Both halves keep input order. Keys must be
    unique: a duplicate would make membership depend on order.
    """
    if not 0.0 <= dev_fraction <= 1.0:
        raise ValueError(f"dev_fraction must be within [0, 1], got {dev_fraction}")
    keys = [key(item) for item in items]
    if len(set(keys)) != len(keys):
        raise ValueError("split keys must be unique")
    groups: dict[Hashable, list[int]] = {}
    for i, item in enumerate(items):
        groups.setdefault(None if stratum is None else stratum(item), []).append(i)
    dev_idx: set[int] = set()
    for members in groups.values():
        quota = math.floor(dev_fraction * len(members) + 0.5)
        dev_idx.update(sorted(members, key=lambda i: _split_rank(keys[i]))[:quota])
    dev = [item for i, item in enumerate(items) if i in dev_idx]
    test = [item for i, item in enumerate(items) if i not in dev_idx]
    return dev, test


def wilson(k: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% interval for a proportion ``k/n``. Honest at small n and near 0/1.

    ``k`` is passed as a float (rate*n rounded) since the caller carries rates, not counts.
    Returns ``(lo, hi)``; a zero-width span for ``n == 0``.
    """
    if n <= 0:
        return 0.0, 0.0
    k = max(0.0, min(float(n), round(k)))
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def mcnemar_exact(b: int, c: int) -> float:
    """Exact two-sided McNemar p-value from discordant pair counts.

    ``b`` = queries backend A got right and B wrong; ``c`` = the reverse.
    Concordant pairs carry no information, so this is an exact sign test on
    the discordant pairs — honest at the small counts this bench produces.
    Returns 1.0 when there are no discordant pairs.
    """
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2.0**n
    return min(1.0, 2.0 * tail)


def bootstrap_stat_ci(
    n: int, stat: Callable[[list[int]], float | None], iters: int = 10_000, seed: int = 0
) -> tuple[float, float]:
    """Seeded percentile-bootstrap 95% CI for any statistic of a resample of ``n`` items.

    ``stat`` receives the resampled indices. A resample on which the statistic is undefined
    (``None`` — e.g. an AUROC draw with no positives) is skipped rather than guessed.
    """
    if n <= 0:
        return 0.0, 0.0
    rng = random.Random(seed)
    values = []
    for _ in range(iters):
        value = stat([rng.randrange(n) for _ in range(n)])
        if value is not None:
            values.append(value)
    if not values:
        return 0.0, 0.0
    values.sort()
    return values[int(0.025 * len(values))], values[int(0.975 * len(values))]


def bootstrap_ci(deltas: list[float], iters: int = 10_000, seed: int = 0) -> tuple[float, float]:
    """Seeded percentile-bootstrap 95% CI for the mean of per-query deltas."""
    n = len(deltas)
    return bootstrap_stat_ci(n, lambda idx: sum(deltas[i] for i in idx) / n, iters, seed)


def auroc(scores: Sequence[float], labels: Sequence[bool]) -> float | None:
    """Area under the ROC curve — P(a random positive outscores a random negative), ties half.

    Rank-based (Mann-Whitney U with average ranks), so it is invariant to any monotone
    rescaling of ``scores``: it measures *discrimination*, not calibration. ``None`` when
    either class is empty (the statistic is undefined, not 0.5).
    """
    pos = sum(1 for y in labels if y)
    neg = len(labels) - pos
    if pos == 0 or neg == 0:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        average = (i + j) / 2 + 1  # 1-based average rank of the tie block
        for t in range(i, j + 1):
            ranks[order[t]] = average
        i = j + 1
    rank_sum = sum(r for r, y in zip(ranks, labels) if y)
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def brier(probs: Sequence[float], labels: Sequence[bool]) -> float:
    """Mean squared error of probabilities against 0/1 outcomes (0 = perfect)."""
    if not probs:
        return 0.0
    return sum((p - (1.0 if y else 0.0)) ** 2 for p, y in zip(probs, labels)) / len(probs)


def reliability_bins(probs: Sequence[float], labels: Sequence[bool], bins: int = 10) -> list[dict]:
    """Equal-width bins of predicted probability: count, mean prediction, observed rate."""
    table = [{"lo": b / bins, "hi": (b + 1) / bins, "n": 0, "p_sum": 0.0, "y_sum": 0} for b in range(bins)]
    for p, y in zip(probs, labels):
        row = table[min(bins - 1, max(0, int(p * bins)))]
        row["n"] += 1
        row["p_sum"] += p
        row["y_sum"] += 1 if y else 0
    return [
        {"lo": r["lo"], "hi": r["hi"], "n": r["n"], "mean_p": r["p_sum"] / r["n"], "rate": r["y_sum"] / r["n"]}
        for r in table
        if r["n"]
    ]


def ece(probs: Sequence[float], labels: Sequence[bool], bins: int = 10) -> float:
    """Expected calibration error: count-weighted |mean prediction − observed rate| over bins."""
    if not probs:
        return 0.0
    return sum(r["n"] * abs(r["mean_p"] - r["rate"]) for r in reliability_bins(probs, labels, bins)) / len(probs)


def platt_fit(scores: Sequence[float], labels: Sequence[bool], iters: int = 100) -> tuple[float, float]:
    """Fit ``P(y=1 | s) = sigmoid(a*s + b)`` by Newton's method (Platt scaling).

    Uses Platt's smoothed targets ``(N+ + 1)/(N+ + 2)`` and ``1/(N- + 2)`` so a separable
    sample doesn't drive the slope to infinity. Returns ``(a, b)`` on the raw score scale — the
    fit a shipped ``core.domain.confidence.Calibration`` takes; ``calibrate`` applies it.
    """
    pos = sum(1 for y in labels if y)
    neg = len(labels) - pos
    t_pos, t_neg = (pos + 1) / (pos + 2), 1 / (neg + 2)
    targets = [t_pos if y else t_neg for y in labels]
    a, b = 0.0, math.log((pos + 1) / (neg + 1))
    for _ in range(iters):
        g_a = g_b = h_aa = h_ab = h_bb = 0.0
        for s, t in zip(scores, targets):
            p = sigmoid(a * s + b)
            w = max(p * (1 - p), 1e-12)
            g_a += (p - t) * s
            g_b += p - t
            h_aa += w * s * s
            h_ab += w * s
            h_bb += w
        det = h_aa * h_bb - h_ab * h_ab
        if abs(det) < 1e-18:
            break
        step_a = (h_bb * g_a - h_ab * g_b) / det
        step_b = (h_aa * g_b - h_ab * g_a) / det
        a, b = a - step_a, b - step_b
        if abs(step_a) < 1e-10 and abs(step_b) < 1e-10:
            break
    return a, b


def recall_any_at_k(ranked: Sequence[str], gold: set[str], k: int) -> bool:
    """True when any gold id is in the top ``k`` (LongMemEval's ``recall_any@k``)."""
    return bool(gold & set(ranked[:k]))


def recall_all_at_k(ranked: Sequence[str], gold: set[str], k: int) -> bool:
    """True when every gold id is in the top ``k`` (LongMemEval's ``recall_all@k``)."""
    return bool(gold) and gold <= set(ranked[:k])


def ndcg_at_k(ranked: Sequence[str], gold: set[str], k: int) -> float:
    """Binary-relevance NDCG@k: DCG of the ranking over the ideal DCG (all gold first)."""
    dcg = sum(1.0 / math.log2(rank + 2) for rank, item in enumerate(ranked[:k]) if item in gold)
    ideal = sum(1.0 / math.log2(rank + 2) for rank in range(min(len(gold), k)))
    return dcg / ideal if ideal else 0.0
