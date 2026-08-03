"""Maturation — age-based STM→LTM transfer (Atkinson–Shiffrin consolidation over time).

``replay`` promotes *recalled* short-term facts and rehearsal promotes *re-seen* ones; both
are activity-based, so a fact captured once and never touched again would sit in STM forever.
Maturation is the missing time-based path: once a short-term fact is older than
``stm_max_age_days`` it transfers to LTM regardless of activity, so STM stays a genuinely
short-term buffer rather than an ever-growing pile of one-off facts.

Recall-neutral at the default ``stm_recall_weight`` (STM and LTM score identically), so this
only bounds STM growth — it does not change what recall returns. The tier flip preserves
``last_seen`` (see ``Store.mature_aged_stm``), so the fact's recency is intact for the
forgetting curve. Thin imperative shell: the age cutoff is computed here, the set-based write
lives in the Store (mirrors ``sweep`` / ``displace`` / ``purge``).
"""

from __future__ import annotations

import time


def mature(store, cfg, project, now: float | None = None) -> int:
    """Transfer STM facts older than ``stm_max_age_days`` into LTM. Returns the number
    matured (0 when disabled with ``stm_max_age_days <= 0``)."""
    if cfg.stm_max_age_days <= 0:
        return 0
    now = now if now is not None else time.time()
    cutoff = now - cfg.stm_max_age_days * 86400
    return store.mature_aged_stm(project["key"], cutoff)
