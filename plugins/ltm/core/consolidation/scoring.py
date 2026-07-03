"""Retention score — how important is a fact, for the sleep pass (design §3A).

Pure Functional Core: features are gathered by the shell (a fact row + a couple of
store lookups) and passed in; this module only does math, so it is stdlib-testable
with no I/O. Reuses the existing ranking primitives in ``core.domain.scoring`` (one
authoritative log-saturation / decay curve) rather than re-deriving them.

    R = w_use·use + w_recency·recency + w_salience·salience
      + w_depth·depth + w_surprise·surprise + w_frequency·frequency

Signal → memory-research mechanism:
  use        recall_count            → retrieval-induced consolidation (testing effect)
  recency    age since last recall/seen → forgetting curve
  salience   corrections / reward     → emotional / dopamine tagging (v2, default 0)
  depth      distiller richness       → levels of processing
  surprise   # facts this superseded  → novelty / prediction error
  frequency  capture frequency        → consolidation
"""

from __future__ import annotations

from dataclasses import dataclass

from core.domain.scoring import frequency_boost, recency_decay


@dataclass(frozen=True)
class RetentionWeights:
    use: float = 0.3
    recency: float = 0.3
    salience: float = 0.2
    depth: float = 0.1
    surprise: float = 0.1
    frequency: float = 0.2


# Authoritative default weights. Tuned by `ltm eval` before pruning is enabled
# (Phase 4b) — kept in one place so a change is a single edit.
DEFAULT_WEIGHTS = RetentionWeights()


@dataclass(frozen=True)
class RetentionFeatures:
    """Raw per-fact signals for the retention score. Gathered by the shell."""

    frequency: int = 1
    recall_count: int = 0
    last_seen: float = 0.0
    last_recalled: float | None = None
    depth: float = 0.0  # 0..1 encoding richness (distiller structure present)
    surprise: int = 0  # number of facts this one superseded
    salience: float = 0.0  # corrections / reward proxy (v2)


def depth_of(row) -> float:
    """Encoding richness (levels of processing): fraction of {title, narrative, type} present."""
    present = sum(1 for col in ("title", "narrative", "type") if (row[col] if col in row.keys() else None))
    return present / 3.0


def features_from_row(row, *, surprise: int = 0) -> RetentionFeatures:
    """Map a fact row → retention features. Pure; ``surprise`` is looked up by the shell."""
    return RetentionFeatures(
        frequency=row["frequency"] or 1,
        recall_count=row["recall_count"] or 0,
        last_seen=row["last_seen"] if row["last_seen"] is not None else (row["created_at"] or 0.0),
        last_recalled=row["last_recalled"],
        depth=depth_of(row),
        surprise=surprise,
        salience=0.0,
    )


def retention(
    f: RetentionFeatures,
    now: float,
    half_life_days: float,
    weights: RetentionWeights = DEFAULT_WEIGHTS,
) -> float:
    """Composite retention score R (higher = keep). Pure — clock passed in, no I/O."""
    use = frequency_boost(f.recall_count + 1)  # +1 so a first recall counts (boost(1)=0)
    last_touch = f.last_recalled if f.last_recalled is not None else f.last_seen
    recency = recency_decay(max(0.0, now - last_touch), half_life_days)
    depth = max(0.0, min(1.0, f.depth))
    salience = max(0.0, min(1.0, f.salience))
    surprise = frequency_boost(f.surprise + 1)
    freq = frequency_boost(f.frequency)
    w = weights
    return (
        w.use * use
        + w.recency * recency
        + w.salience * salience
        + w.depth * depth
        + w.surprise * surprise
        + w.frequency * freq
    )
