"""The consolidation ("sleep") pipeline — replay / displace / refine / purge.

Mirrors active systems consolidation: a discrete, off-hot-path pass that promotes
rehearsed short-term facts (replay), displaces the weakest short-term overflow
(STM capacity), prunes low-importance ones (refine, SHY), then hard-removes long-
archived rows (purge). What it keeps vs forgets is decided by the pure retention
score in scoring.py (design section 3A). Each retrieval-affecting step is gated
default-off until eval-tuned, and archival is reversible (status flip, not delete).
"""

from __future__ import annotations


def consolidate(store, cfg, project, now: float | None = None) -> dict[str, int]:
    """Run one consolidation pass and return per-stage counts.

    The imperative shell (``bin/capture.py``) calls this at session checkpoints —
    like sleep, not every turn. Ordered so nothing is lost: replay first (rehearsed
    STM → LTM, so those rows leave the STM overflow set), then STM displacement,
    then the global retention prune, then the time-based hard purge of already-
    archived rows. Every step is individually gated/idempotent and reversible
    (displacement/prune archive; only purge deletes, and only long-archived rows),
    so a pass is safe to repeat.
    """
    from core.consolidation.refine import refine
    from core.consolidation.replay import replay

    promoted = replay(store, project, now)
    displaced = store.displace_stm(project["key"], cfg.stm_capacity) if cfg.stm_capacity > 0 else 0
    pruned = refine(store, cfg, project, now)
    purged = store.purge(cfg.purge_horizon_days * 86400, now) if cfg.purge_horizon_days > 0 else 0
    return {"promoted": promoted, "displaced": displaced, "pruned": pruned, "purged": purged or 0}
