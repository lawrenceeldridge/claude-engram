"""Refine — SHY-style forgetting: prune low-importance facts so the active set stays small.

Computes the retention score (design §3A) for each active fact and archives the weakest
(``status='pruned'`` — reversible; recall scans 'active' only). This is the scale-control
that keeps brute-force search viable (design §8A). Three gated knobs, each one meaning:

- ``refine_keep_max`` — keep only the top-N by retention, prune the rest. An absolute
  count, so it is **idempotent**: a second pass finds exactly N active and prunes nothing.
  Ships **on** as a generous, non-destructive backstop (fires only on runaway growth).
- ``refine_prune_percentile`` — a value strictly in ``(0, 1)`` drops the weakest that
  *fraction* of the current active set (cohort-relative, so the cut scales with the live
  population — the SHY "only the relatively strong survive" property, achieved statelessly).
- ``refine_min_retention`` — the **forgetting curve's absolute retention floor**: prune every
  fact whose retention score is below it. Because retention decays with dormancy (recency)
  but is lifted by recall (``use``), reinforcement (``frequency``) and importance
  (``salience``), this is exactly "a fact fades over time **unless** recalled, reinforced, or
  important". Idempotent (an absolute floor, recomputed from stored features each pass).

**On repeat semantics.** The percentile is applied *per pass*, so repeated passes prune
further — it **converges** rather than being strictly idempotent. ``keep_max`` and
``min_retention`` are idempotent absolutes. All are stateless and eval-reproducible: the cut
is recomputed from stored features each pass, never from a persisted running score, so it
never double-counts recency and a single pass is deterministic given the store. (Unlike a
multiplicative SHY downscale, rejected — see DESIGN §3A.)

**Retrieval-affecting, so the two destructive knobs (``prune_percentile``, ``min_retention``)
default off** until tuned; ``keep_max`` ships on as a non-destructive ceiling. Note `engram
eval` is a recall-only benchmark and does **not** exercise this consolidation path, so the
floor is validated by unit tests + reasoning, not the benchmark. Scoring is pure; the I/O
(row reads, status writes, the supersede-count lookup) lives here in the shell.
"""

from __future__ import annotations

import math
import time

from core.consolidation.scoring import DEFAULT_WEIGHTS, RetentionWeights, features_from_row, retention


def refine(store, cfg, project, now: float | None = None, weights: RetentionWeights = DEFAULT_WEIGHTS) -> int:
    """Archive the lowest-retention active facts. Returns the number pruned (0 if disabled)."""
    keep_max = cfg.refine_keep_max
    pct = cfg.refine_prune_percentile
    min_retention = cfg.refine_min_retention
    if keep_max <= 0 and pct <= 0 and min_retention <= 0:
        return 0  # disabled — behaviour + eval unchanged

    now = now if now is not None else time.time()
    scored: list[tuple[float, str]] = []
    surprise = store.supersede_counts()  # one scan for the whole pass, not one per fact
    for row in store.active_rows_for_project(project["key"]):
        # Anti-patterns are standing rules, not decaying observations — exempt from
        # dormancy-based pruning. They are invalidated only by supersession or the drift
        # stage, never by low retention; otherwise a rarely-recalled lesson would be pruned
        # precisely when it has been dormant long enough for the model to need reminding.
        if row["kind"] == "antipattern":
            continue
        feats = features_from_row(row, surprise=surprise.get(row["id"], 0))
        scored.append((retention(feats, now, cfg.half_life_days, weights), row["id"]))
    scored.sort(key=lambda pair: pair[0])  # weakest first

    to_prune: set[str] = set()
    if 0 < pct < 1:
        # Cohort-relative percentile — drop the weakest ``pct`` fraction of the live active
        # set (rounding up, so a non-zero fraction of a non-empty store prunes at least one).
        cut = math.ceil(pct * len(scored))
        to_prune |= {fid for _score, fid in scored[:cut]}
    if min_retention > 0:
        # The forgetting curve — an absolute retention floor: a fact fades once its score has
        # decayed below it, unless recall / reinforcement / salience keep it above.
        to_prune |= {fid for score, fid in scored if score < min_retention}
    if keep_max > 0 and len(scored) > keep_max:
        overflow = len(scored) - keep_max
        to_prune |= {fid for _score, fid in scored[:overflow]}
    return store.set_status(list(to_prune), "pruned")
