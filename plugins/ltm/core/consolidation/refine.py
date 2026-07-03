"""Refine — REM/SHY: prune low-importance facts so the active set stays small.

Computes the retention score (design §3A) for each active fact and archives the
weakest (``status='pruned'`` — reversible; recall scans 'active' only). This is the
scale-control that keeps brute-force search viable (design §8A): a relative cap
(``retention_keep_max``) is self-limiting; an absolute floor (``prune_threshold``)
drops clear junk.

**Retrieval-affecting, so gated default-off** (both knobs 0 → no-op) until the
weights are `ltm eval`-tuned (Phase 4b). Scoring is pure; the I/O (row reads, status
writes, the supersede-count lookup) lives here in the shell.
"""

from __future__ import annotations

import time

from core.consolidation.scoring import DEFAULT_WEIGHTS, RetentionWeights, features_from_row, retention


def refine(store, cfg, project, now: float | None = None, weights: RetentionWeights = DEFAULT_WEIGHTS) -> int:
    """Archive the lowest-retention active facts. Returns the number pruned (0 if disabled)."""
    keep_max = getattr(cfg, "retention_keep_max", 0)
    threshold = getattr(cfg, "prune_threshold", 0.0)
    if keep_max <= 0 and threshold <= 0:
        return 0  # disabled — behaviour + eval unchanged

    now = now if now is not None else time.time()
    scored: list[tuple[float, str]] = []
    for row in store.active_rows_for_project(project["key"]):
        feats = features_from_row(row, surprise=store.supersede_count(row["id"]))
        scored.append((retention(feats, now, cfg.half_life_days, weights), row["id"]))
    scored.sort(key=lambda pair: pair[0])  # weakest first

    to_prune: set[str] = set()
    if threshold > 0:
        to_prune |= {fid for score, fid in scored if score < threshold}
    if keep_max > 0 and len(scored) > keep_max:
        overflow = len(scored) - keep_max
        to_prune |= {fid for _score, fid in scored[:overflow]}
    return store.set_status(list(to_prune), "pruned")
