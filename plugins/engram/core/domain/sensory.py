"""Pure sensory-register decisions (Functional Core).

Atkinson & Shiffrin (1968) split *structures* (the stores) from *control processes* — the
transient, subject-controlled strategies that move information between them. The sensory
register is the single intake stage all perception enters; its one control process here is
**attention** — a selective read-out that transfers a perception to the durable store. This
module holds that one pure decision. No I/O, no clock: the register table (``Store``) does the
storing and the SQL decay sweep; the intake shell sets ``attended``.
"""

from __future__ import annotations

from typing import Any


def should_promote(row: Any) -> bool:
    """True when a perception should transfer out of the sensory register into the durable
    store — the A-S **attention** gate: it has been *attended* (selectively read out) and is
    still live (``decayed_at`` is unset, i.e. it has not decayed or already left).

    Attention is set by the intake shell (visual: re-perception of the same page; verbal:
    distillation-worthiness), never by a rehearsal/frequency count — that is the STM->LTM
    path (``store.reinforce`` / ``store.promote``), which perceptions must not reuse.

    ``row`` is any mapping with ``attended`` and ``decayed_at`` (a ``sqlite3.Row`` or dict).
    """
    return bool(row["attended"]) and row["decayed_at"] is None
