"""Idea #5 — use-feedback inhibition (Engle/Kane). Inert core: a pure retention penalty
for injected-but-unused facts, plus the store tallies that feed it. The inhibition weight
defaults to 0, so ranking is unchanged until a "used" detector is wired and eval-tuned."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import service
from core.config import get_config
from core.consolidation.scoring import (
    RetentionFeatures,
    RetentionWeights,
    features_from_row,
    inhibition_signal,
    retention,
)
from core.ports.embedding import HashEmbedding
from core.store import Store


class InhibitionSignalTests(unittest.TestCase):
    def test_never_injected_is_zero(self):
        self.assertEqual(inhibition_signal(0, 0), 0.0)

    def test_always_used_is_zero(self):
        self.assertEqual(inhibition_signal(5, 5), 0.0)

    def test_never_used_is_one(self):
        self.assertEqual(inhibition_signal(5, 0), 1.0)

    def test_partial_use_is_fraction(self):
        self.assertAlmostEqual(inhibition_signal(4, 1), 0.75)

    def test_bounded_when_used_exceeds_injected(self):
        self.assertEqual(inhibition_signal(2, 5), 0.0)


class RetentionInhibitionTests(unittest.TestCase):
    def test_weight_zero_is_inert(self):
        # Default weights carry inhibition=0, so an unused fact scores the same as a used one.
        base = RetentionFeatures(frequency=1, recall_count=0)
        unused = RetentionFeatures(frequency=1, recall_count=0, inhibition=1.0)
        self.assertEqual(retention(base, now=0.0, half_life_days=30), retention(unused, now=0.0, half_life_days=30))

    def test_penalty_de_ranks_when_weighted(self):
        w = RetentionWeights(inhibition=0.5)
        used = RetentionFeatures(frequency=1, recall_count=0, inhibition=0.0)
        ignored = RetentionFeatures(frequency=1, recall_count=0, inhibition=1.0)
        self.assertGreater(
            retention(used, now=0.0, half_life_days=30, weights=w),
            retention(ignored, now=0.0, half_life_days=30, weights=w),
        )

    def test_features_from_row_defaults_zero_without_columns(self):
        # An old row (dict without the outcome columns) yields no inhibition — safe upgrade.
        row = {
            "frequency": 1,
            "recall_count": 0,
            "last_seen": 0.0,
            "created_at": 0.0,
            "last_recalled": None,
            "title": "",
            "narrative": "",
            "type": "",
        }
        self.assertEqual(features_from_row(row).inhibition, 0.0)


class OutcomeStoreTests(unittest.TestCase):
    def _store(self):
        tmp = tempfile.mkdtemp(prefix="engram-test-inh-")
        return Store(Path(tmp) / "m.db"), {"key": "inh", "path": tmp, "label": "inh"}

    def test_migration_added_outcome_columns(self):
        store, _ = self._store()
        cols = {r[1] for r in store.db.execute("PRAGMA table_info(facts)").fetchall()}
        store.close()
        self.assertIn("injected_count", cols)
        self.assertIn("used_count", cols)

    def test_mark_injected_and_used_increment(self):
        cfg = get_config()
        embedder = HashEmbedding(dim=cfg.dim)
        store, project = self._store()
        service.add_facts(store, embedder, cfg, project, "s", ["a lone fact about deployment"])
        fid = store.active_rows_for_project(project["key"])[0]["id"]
        store.mark_injected([fid])
        store.mark_injected([fid])
        store.mark_used([fid])
        row = store.active_rows_for_project(project["key"])[0]
        store.close()
        self.assertEqual(row["injected_count"], 2)
        self.assertEqual(row["used_count"], 1)


if __name__ == "__main__":
    unittest.main()
