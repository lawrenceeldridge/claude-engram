"""Consolidation tests (Phase 4 of stm-ltm-membus).

Retention score (pure), replay (promote recalled STM), refine (SHY prune — gated
default-off), and the two-stage purge. Stdlib unittest, hash embedder, no network.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import service  # noqa: E402
from core.config import get_config  # noqa: E402
from core.consolidation.refine import refine  # noqa: E402
from core.consolidation.replay import replay  # noqa: E402
from core.consolidation.scoring import RetentionFeatures, depth_of, retention  # noqa: E402
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.store import Store  # noqa: E402

NOW = 1_000_000.0
HL = 30.0


class RetentionScoreTests(unittest.TestCase):
    """Pure score — monotonic in each signal, holding the rest fixed."""

    def _base(self, **kw):
        return replace(RetentionFeatures(frequency=1, recall_count=0, last_seen=NOW), **kw)

    def _r(self, f):
        return retention(f, NOW, HL)

    def test_more_recall_scores_higher(self):
        self.assertGreater(self._r(self._base(recall_count=5)), self._r(self._base()))

    def test_higher_frequency_scores_higher(self):
        self.assertGreater(self._r(self._base(frequency=8)), self._r(self._base()))

    def test_more_recent_scores_higher(self):
        recent = self._base(last_seen=NOW)
        old = self._base(last_seen=NOW - 100 * 86400)
        self.assertGreater(self._r(recent), self._r(old))

    def test_recall_recency_beats_capture_recency(self):
        # A fact recalled recently but captured long ago is still "fresh".
        f = self._base(last_seen=NOW - 100 * 86400, last_recalled=NOW)
        self.assertGreater(self._r(f), self._r(self._base(last_seen=NOW - 100 * 86400)))

    def test_depth_and_surprise_score_higher(self):
        self.assertGreater(self._r(self._base(depth=1.0)), self._r(self._base()))
        self.assertGreater(self._r(self._base(surprise=5)), self._r(self._base()))

    def test_depth_of_counts_structure(self):
        self.assertEqual(depth_of({"title": "", "narrative": "", "type": ""}), 0.0)
        self.assertAlmostEqual(depth_of({"title": "x", "narrative": "", "type": ""}), 1 / 3)
        self.assertEqual(depth_of({"title": "x", "narrative": "y", "type": "z"}), 1.0)


class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LTM_DATA_DIR"] = self.tmp.name
        self.cfg = replace(get_config(), distiller="heuristic")
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "test", "path": "/tmp/test", "label": "test"}

    def tearDown(self):
        self.store.close()
        os.environ.pop("LTM_DATA_DIR", None)
        self.tmp.cleanup()

    def _add(self, text):
        service.add_facts(self.store, self.embedder, self.cfg, self.project, "s1", [text])
        return self.store.fact_id(self.project["key"], text)

    # --- replay ---

    def test_replay_promotes_recalled_stm(self):
        used = self._add("used fact")
        unused = self._add("unused fact")
        self.store.mark_recalled([used])  # recall_count -> 1
        promoted = replay(self.store, self.project)
        self.assertEqual(promoted, 1)
        self.assertEqual(self.store.get(used)["tier"], "ltm")
        self.assertEqual(self.store.get(unused)["tier"], "stm")  # not recalled -> stays STM

    # --- refine (gated) ---

    def test_refine_is_noop_when_disabled(self):
        for i in range(4):
            self._add(f"fact {i}")
        self.assertEqual(refine(self.store, self.cfg, self.project), 0)
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 4)

    def test_refine_keep_max_prunes_weakest(self):
        for i in range(5):
            self._add(f"fact {i}")
        cfg = replace(self.cfg, retention_keep_max=3)
        pruned = refine(self.store, cfg, self.project)
        self.assertEqual(pruned, 2)
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 3)

    def test_refine_threshold_prunes_below_floor(self):
        for i in range(3):
            self._add(f"fact {i}")
        cfg = replace(self.cfg, prune_threshold=10.0)  # absurdly high -> everything is below
        pruned = refine(self.store, cfg, self.project)
        self.assertEqual(pruned, 3)
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 0)

    # --- purge (two-stage lifecycle) ---

    def test_purge_deletes_long_archived_only(self):
        keep_active = self._add("active fact")
        recent_pruned = self._add("recent pruned")
        old_pruned = self._add("old pruned")
        self.store.set_status([recent_pruned], "pruned")
        self.store.set_status([old_pruned], "pruned")
        # Age the old one well past the horizon.
        self.store.db.execute("UPDATE facts SET last_seen = ? WHERE id = ?", (NOW - 10_000, old_pruned))
        self.store.db.commit()
        deleted = self.store.purge(horizon_seconds=5_000, now=NOW)
        self.assertEqual(deleted, 1)  # only the old archived row
        self.assertIsNone(self.store.get(old_pruned))
        self.assertIsNotNone(self.store.get(recent_pruned))  # within horizon
        self.assertIsNotNone(self.store.get(keep_active))  # active never purged


if __name__ == "__main__":
    unittest.main()
