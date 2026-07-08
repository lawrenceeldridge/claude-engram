"""Pure sensory-register decisions (Functional Core).

The sensory tier's one genuinely-pure decision: whether an ephemeral snapshot has been
"attended" enough to promote into STM. This is the rehearsal analogue of the STM→LTM
``promote_after_freq`` — a page re-glanced enough times is attended. No I/O, no clock.

(Decay is a simple capacity + TTL SQL sweep in ``Store.sweep_sensory``, mirroring the
facts TTL sweep, so it needs no pure selector here.)
"""

from __future__ import annotations

from typing import Any


def should_promote(row: Any, promote_after: int) -> bool:
    """True when a sensory snapshot is "attended" — re-glanced at least ``promote_after``
    times and not already promoted. ``row`` is any keyed mapping with ``glance_count`` and
    ``attended`` (a ``sqlite3.Row`` or a dict)."""
    if row["attended"]:
        return False
    return int(row["glance_count"]) >= max(1, promote_after)
