"""The consolidation ("sleep") pipeline — replay / displace / integrate / refine / purge.

Mirrors active systems consolidation + the Sequential Hypothesis: a discrete,
off-hot-path pass that promotes rehearsed short-term facts (replay), displaces the
weakest short-term overflow (STM capacity), collapses near-duplicates (integrate — the
REM-style integration floor), prunes low-importance ones (refine, SHY), then hard-removes
long-archived rows (purge). What it keeps vs forgets is decided by the pure retention
score in scoring.py (design section 3A). Each retrieval-affecting step is gated default-off
until eval-tuned, and archival is reversible (status flip, not delete).

The RNR model's *rescue* stage (re-distil parked degraded deltas) is deliberately
NOT here: it needs the embedder + distiller and runs at the head of every capture,
so it lives in ``core/service.py::rescue``, co-located with the write path rather
than in this checkpoint-only pass.
"""

from __future__ import annotations


def consolidate(store, cfg, project, now: float | None = None, embedder=None) -> dict[str, int]:
    """Run one consolidation pass and return per-stage counts.

    The imperative shell (``bin/capture.py``) calls this at session checkpoints — like
    sleep, not every turn. Order is **data-safety-driven, not biological-phase-mimicry**:
    replay first (rehearsed STM → LTM, so those rows leave the STM overflow set), then STM
    displacement, then integrate (dedup near-duplicates before the retention cut scores
    them), then the retention prune, then the time-based hard purge of already-archived
    rows. replay/displace/integrate and the keep_max/absolute-floor refine modes are
    idempotent; the refine *percentile* mode is per-pass/convergent (see refine.py). All
    archival is reversible (only purge deletes, and only long-cold rows).
    """
    from core.consolidation.integrate import integrate
    from core.consolidation.invalidate import invalidate_stale_antipatterns
    from core.consolidation.refine import refine
    from core.consolidation.replay import replay

    promoted = replay(store, project, now)
    displaced = store.displace_stm(project["key"], cfg.stm_capacity) if cfg.stm_capacity > 0 else 0
    merged = integrate(store, cfg, project, now, embedder=embedder)
    pruned = refine(store, cfg, project, now)
    # Anti-patterns are dormancy-exempt (refine/sweep skip them); they leave the active set
    # only by supersession or here, when the files they warn about no longer exist on disk.
    invalidated = invalidate_stale_antipatterns(store, project, now)
    purged = store.purge(cfg.purge_horizon_days * 86400, now) if cfg.purge_horizon_days > 0 else 0
    return {
        "promoted": promoted,
        "displaced": displaced,
        "merged": merged,
        "pruned": pruned,
        "invalidated": invalidated,
        "purged": purged or 0,
    }
