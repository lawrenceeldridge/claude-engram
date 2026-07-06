"""Refine — SHY-style forgetting: prune low-importance facts so the active set stays small.

Computes the retention score (design §3A) for each active fact and archives the weakest
(``status='pruned'`` — reversible; recall scans 'active' only). This is the scale-control
that keeps brute-force search viable (design §8A). Two gated knobs, both default-off (0):

- ``refine_keep_max`` — keep only the top-N by retention, prune the rest. An absolute
  count, so it is **idempotent**: a second pass finds exactly N active and prunes nothing.
- ``refine_prune_percentile`` — a value in ``(0, 1)`` drops the weakest that *fraction* of
  the current active set (cohort-relative, so the cut scales with the live population — the
  SHY "only the relatively strong survive" property, achieved statelessly). A value ``>= 1``
  is an absolute retention-score floor (idempotent).

**On repeat semantics.** The percentile is applied *per pass*, so repeated passes prune
further — it **converges** rather than being strictly idempotent. That is acceptable
because consolidation runs at sparse checkpoints and archival is reversible; and unlike a
multiplicative SHY downscale (rejected — see DESIGN §3A), it stays **stateless and
eval-reproducible**: the cut is recomputed from stored features each pass, never from a
persisted running score, so it never double-counts recency and a single pass is
deterministic given the store.

**Retrieval-affecting, so gated default-off** (both knobs 0 → no-op) until the weights are
`ltm eval`-tuned. Scoring is pure; the I/O (row reads, status writes, the supersede-count
lookup) lives here in the shell.
"""

from __future__ import annotations

import math
import time

from core.consolidation.scoring import DEFAULT_WEIGHTS, RetentionWeights, features_from_row, retention


def refine(store, cfg, project, now: float | None = None, weights: RetentionWeights = DEFAULT_WEIGHTS) -> int:
    """Archive the lowest-retention active facts. Returns the number pruned (0 if disabled)."""
    keep_max = cfg.refine_keep_max
    pct = cfg.refine_prune_percentile
    if keep_max <= 0 and pct <= 0:
        return 0  # disabled — behaviour + eval unchanged

    now = now if now is not None else time.time()
    scored: list[tuple[float, str]] = []
    for row in store.active_rows_for_project(project["key"]):
        # Anti-patterns are standing rules, not decaying observations — exempt from
        # dormancy-based pruning. They are invalidated only by supersession or the drift
        # stage, never by low retention; otherwise a rarely-recalled lesson would be pruned
        # precisely when it has been dormant long enough for the model to need reminding.
        if row["kind"] == "antipattern":
            continue
        feats = features_from_row(row, surprise=store.supersede_count(row["id"]))
        scored.append((retention(feats, now, cfg.half_life_days, weights), row["id"]))
    scored.sort(key=lambda pair: pair[0])  # weakest first

    to_prune: set[str] = set()
    if 0 < pct < 1:
        # Cohort-relative percentile — drop the weakest ``pct`` fraction of the live active
        # set (rounding up, so a non-zero fraction of a non-empty store prunes at least one).
        cut = math.ceil(pct * len(scored))
        to_prune |= {fid for _score, fid in scored[:cut]}
    elif pct >= 1:
        # Absolute retention-score floor.
        to_prune |= {fid for score, fid in scored if score < pct}
    if keep_max > 0 and len(scored) > keep_max:
        overflow = len(scored) - keep_max
        to_prune |= {fid for _score, fid in scored[:overflow]}
    return store.set_status(list(to_prune), "pruned")
