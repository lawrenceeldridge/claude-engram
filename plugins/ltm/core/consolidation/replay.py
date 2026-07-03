"""Replay — NREM active systems consolidation: rehearsed short-term facts graduate.

A short-term fact that has actually been *used* (recalled at least once) has proven
its worth, so replay promotes it to the long-term store — the batch counterpart to
the inline rehearsal promotion in ``service.add_records``. Additive and safe: it only
moves STM→LTM (recall treats both tiers alike by default), never removes anything.
"""

from __future__ import annotations

import time


def replay(store, project, now: float | None = None) -> int:
    """Promote recalled short-term facts to long-term. Returns the number promoted."""
    now = now if now is not None else time.time()
    promoted = 0
    for row in store.stm_rows(project["key"]):
        if (row["recall_count"] or 0) >= 1:
            store.promote(row["id"], now)
            promoted += 1
    return promoted
