"""The consolidation ("sleep") pipeline — replay / mature / displace / integrate / refine /
invalidate / purge / forget.

Mirrors active systems consolidation + the Sequential Hypothesis: a discrete,
off-hot-path pass that promotes rehearsed short-term facts (replay), matures short-term
facts past an age horizon into LTM (mature — the time-based promotion path), displaces the
weakest short-term overflow (STM capacity), collapses near-duplicates (integrate — the
REM-style integration floor), prunes low-importance ones (refine, SHY), retires anti-patterns
whose files are gone (invalidate), hard-removes long-archived rows (purge), and forgets verbatim
exchanges past their retention limits (forget). What it keeps vs forgets among facts is decided
by the pure retention score in scoring.py (design section 3A). Each retrieval-affecting step is
gated default-off until eval-tuned, and fact archival is reversible (status flip, not delete).

The RNR model's *rescue* stage (re-distil parked degraded deltas) is deliberately
NOT here: it needs the embedder + distiller and runs at the head of every capture,
so it lives in ``core/service.py::rescue``, co-located with the write path rather
than in this checkpoint-only pass.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

# Each stage runs under this deadline (Store.deadline): ~35× the slowest stage measured on a
# 144k-fact store after #67 (refine 1.8 s), so it never trips on a healthy pass — it exists so
# that a stage gone quadratic (the 13.5 h refine) costs a minute, not a night. An interrupted
# stage rolls back, is counted (``interrupted``) and logged; the next pass runs it again.
STAGE_DEADLINE_SECONDS = 60.0


def stages(store, cfg, project, now: float | None = None, embedder=None) -> tuple[tuple[str, Callable[[], int]], ...]:
    """The pass's stages in order, each ``(count key, run)`` — the one definition ``consolidate``
    runs and the scale tests measure."""
    from core.consolidation.integrate import integrate
    from core.consolidation.invalidate import invalidate_stale_antipatterns
    from core.consolidation.mature import mature
    from core.consolidation.refine import refine
    from core.consolidation.replay import replay

    def forget() -> int:
        # Episodic forgetting: verbatim exchanges past the retention horizon or beyond the
        # per-project cap leave the index (an on-demand trace, not facts — a hard delete, like
        # purge). Facts outlive their exchanges, so a fact whose whole episode is gone loses its link.
        forgotten = store.prune_nonfile_chunks(
            project["key"],
            "exchange",
            max_age_seconds=cfg.episodic_ttl_days * 86400,
            keep_max=cfg.episodic_max_chunks,
            now=now,
        )
        if forgotten:
            store.unlink_forgotten_episodes(project["key"])
        return forgotten

    return (
        ("promoted", lambda: replay(store, project, now)),
        ("matured", lambda: mature(store, cfg, project, now)),
        ("displaced", lambda: store.displace_stm(project["key"], cfg.stm_capacity) if cfg.stm_capacity > 0 else 0),
        ("merged", lambda: integrate(store, cfg, project, now, embedder=embedder)),
        ("pruned", lambda: refine(store, cfg, project, now)),
        # Anti-patterns are dormancy-exempt (refine/sweep skip them); they leave the active set
        # only by supersession or here, when the files they warn about no longer exist on disk.
        ("invalidated", lambda: invalidate_stale_antipatterns(store, project, now)),
        ("purged", lambda: store.purge(cfg.purge_horizon_days * 86400, now) if cfg.purge_horizon_days > 0 else 0),
        ("forgotten", forget),
    )


def consolidate(store, cfg, project, now: float | None = None, embedder=None) -> dict[str, int]:
    """Run one consolidation pass and return per-stage counts (plus ``interrupted``: stages cut
    off by :data:`STAGE_DEADLINE_SECONDS`).

    The imperative shell (``bin/capture.py``) calls this at session checkpoints — like
    sleep, not every turn. Order is **data-safety-driven, not biological-phase-mimicry**:
    replay first (rehearsed/recalled STM → LTM), then maturation (age-based STM → LTM), so
    both promotion paths run before displacement and those rows leave the STM overflow set;
    then STM displacement, then integrate (dedup near-duplicates before the retention cut
    scores them), then the retention prune, then anti-pattern invalidation, then the time-based
    hard purge of already-archived rows, then episodic forgetting (verbatim exchanges past
    ``episodic_ttl_days`` or the cap). replay/mature/displace/integrate and the
    keep_max/absolute-floor refine modes are idempotent; the refine *percentile* mode is
    per-pass/convergent (see refine.py) — so a stage cut short just resumes next pass. Fact
    archival is reversible; only two stages delete — purge (long-cold archived facts) and forget
    (verbatim exchanges, an on-demand trace).
    """
    from core import errlog

    counts: dict[str, int] = {}
    interrupted = []
    for key, stage in stages(store, cfg, project, now, embedder):
        try:
            with store.deadline(STAGE_DEADLINE_SECONDS):
                counts[key] = stage() or 0
        except sqlite3.OperationalError as exc:
            if "interrupted" not in str(exc):
                raise
            counts[key] = 0
            interrupted.append(key)
    if interrupted:
        errlog.record(
            cfg.data_dir,
            "consolidate",
            f"{project.get('label') or project['key']}: {', '.join(interrupted)} exceeded "
            f"{STAGE_DEADLINE_SECONDS:.0f} s and was interrupted (rolled back; the next pass runs it again)",
        )
    counts["interrupted"] = len(interrupted)
    return counts
