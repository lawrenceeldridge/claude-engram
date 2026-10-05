"""Time windows over stored episodes (Functional Core — pure).

When a question names a time ("what did we decide last week?"), the model translates it into
a window and ``search_history`` boosts the conversations inside it. Pure: the window is a Value
Object over epoch seconds and the boost is arithmetic on fused scores — no clock, no date
parsing (the composition root turns ISO dates into epoch seconds), no I/O.

Why a multiplicative boost and not one more rank-fusion channel: Reciprocal Rank Fusion's
smoothing (60) flattens rank gaps — neighbours differ by ~1.6% — so a ``window`` channel as
heavy as similarity could not even break an exact tie the other channels agreed on. Scaling the
fused score moves a candidate a bounded distance instead: in-window ×(1 + ``WINDOW_BOOST``),
decaying by half every ``WINDOW_HALF_LIFE`` outside, so a slightly wrong window degrades
gracefully.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from core.domain.fusion import Fused

WINDOW_BOOST = 0.4  # mempalace's published within-window factor (+40%) — external, not fitted here
WINDOW_HALF_LIFE = 7 * 86400.0  # outside the window the boost halves every week


@dataclass(frozen=True)
class TimeWindow:
    """A closed interval of epoch seconds. Either bound may be open (``None``), not both."""

    after: float | None = None
    before: float | None = None

    def __post_init__(self) -> None:
        if self.after is None and self.before is None:
            raise ValueError("a time window needs an `after` or a `before` bound")
        if self.after is not None and self.before is not None and self.after > self.before:
            raise ValueError("`after` must not be later than `before`")

    def distance(self, ts: float) -> float:
        """Seconds from ``ts`` to the window — 0 inside it."""
        if self.after is not None and ts < self.after:
            return self.after - ts
        if self.before is not None and ts > self.before:
            return ts - self.before
        return 0.0

    def boost(self, ts: float | None) -> float:
        """The score multiplier for something stamped ``ts``: ``1 + WINDOW_BOOST`` inside the window,
        halving every ``WINDOW_HALF_LIFE`` outside it; exactly 1 when there is no stamp."""
        if ts is None:
            return 1.0
        return 1.0 + WINDOW_BOOST * 2.0 ** (-self.distance(ts) / WINDOW_HALF_LIFE)


def boost_by_window(fused: Iterable[Fused], stamps: Mapping[str, float | None], window: TimeWindow) -> list[Fused]:
    """Fused results re-scored by their stamp's proximity to ``window`` (``TimeWindow.boost``) and
    re-sorted, highest first. Soft: it re-orders the candidates it is given and never adds or drops
    one; equal scores keep their given order."""
    boosted = [Fused(f.fact_id, f.score * window.boost(stamps.get(f.fact_id)), dict(f.contributions)) for f in fused]
    return sorted(boosted, key=lambda f: f.score, reverse=True)
