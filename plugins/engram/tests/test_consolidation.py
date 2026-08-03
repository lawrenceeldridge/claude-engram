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
from core.consolidation import consolidate  # noqa: E402
from core.consolidation.mature import mature  # noqa: E402
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
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        # Pin every consolidation lever to its code default (all off) so the suite is hermetic
        # against ambient ENGRAM_* forgetting config in the environment; the tests that exercise a
        # lever override it locally via replace(self.cfg, ...).
        self.cfg = replace(
            get_config(),
            distiller="heuristic",
            stm_capacity=0,
            stm_max_age_days=0,  # maturation off here; MaturationTests exercises it explicitly
            integrate_threshold=0,
            refine_keep_max=0,
            refine_prune_percentile=0,
            purge_horizon_days=0,
        )
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "test", "path": "/tmp/test", "label": "test"}

    def tearDown(self):
        self.store.close()
        os.environ.pop("ENGRAM_DATA_DIR", None)
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
        cfg = replace(self.cfg, refine_keep_max=3)
        pruned = refine(self.store, cfg, self.project)
        self.assertEqual(pruned, 2)
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 3)

    def test_refine_threshold_prunes_below_floor(self):
        for i in range(3):
            self._add(f"fact {i}")
        cfg = replace(self.cfg, refine_prune_percentile=10.0)  # >=1 raw floor, absurdly high -> everything is below
        pruned = refine(self.store, cfg, self.project)
        self.assertEqual(pruned, 3)
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 0)

    def test_refine_percentile_prunes_weakest_fraction(self):
        for i in range(10):
            self._add(f"fact {i}")
        cfg = replace(self.cfg, refine_prune_percentile=0.2)  # 0<p<1 -> drop weakest 20%
        pruned = refine(self.store, cfg, self.project)
        self.assertEqual(pruned, 2)  # ceil(0.2 * 10)
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 8)

    def test_refine_percentile_converges_per_pass(self):
        # Percentile mode is applied per pass (cohort-relative), so a repeat prunes further —
        # self-limiting and stateless, but not strictly idempotent (documented in refine.py).
        for i in range(10):
            self._add(f"fact {i}")
        cfg = replace(self.cfg, refine_prune_percentile=0.2)
        self.assertEqual(refine(self.store, cfg, self.project), 2)  # 10 -> 8
        self.assertEqual(refine(self.store, cfg, self.project), 2)  # 8 -> 6 (ceil(0.2*8))
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 6)

    def test_refine_keep_max_is_idempotent(self):
        # Absolute count -> a second pass finds exactly N active and prunes nothing more.
        for i in range(5):
            self._add(f"fact {i}")
        cfg = replace(self.cfg, refine_keep_max=3)
        self.assertEqual(refine(self.store, cfg, self.project), 2)
        self.assertEqual(refine(self.store, cfg, self.project), 0)
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 3)

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

    # --- consolidate (the orchestration the capture worker calls) ---

    def test_consolidate_displacement_disabled_by_default(self):
        for i in range(5):
            self._add(f"fact {i}")
        counts = consolidate(self.store, self.cfg, self.project)  # stm_capacity defaults to 0
        self.assertEqual(counts["displaced"], 0)
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 5)

    def test_consolidate_displaces_stm_beyond_capacity(self):
        for i in range(5):
            self._add(f"fact {i}")
        cfg = replace(self.cfg, stm_capacity=3)
        counts = consolidate(self.store, cfg, self.project)
        self.assertEqual(counts["displaced"], 2)  # weakest 2 beyond the cap
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 3)
        # Reversible archive, not a delete — the rows still exist as 'displaced'.
        total = self.store.db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.assertEqual(total, 5)
        displaced = self.store.db.execute("SELECT COUNT(*) FROM facts WHERE status = 'displaced'").fetchone()[0]
        self.assertEqual(displaced, 2)

    def test_consolidate_promotes_before_displacing(self):
        # replay runs first: a rehearsed STM fact is promoted to LTM and so is exempt
        # from STM displacement even at capacity 1.
        first = self._add("older recalled fact")
        self._add("newer fact")
        self.store.mark_recalled([first])  # rehearsal signal
        cfg = replace(self.cfg, stm_capacity=1)
        counts = consolidate(self.store, cfg, self.project)
        self.assertEqual(counts["promoted"], 1)
        self.assertEqual(counts["displaced"], 0)  # 1 STM left after promotion == capacity
        self.assertEqual(self.store.get(first)["tier"], "ltm")
        self.assertEqual(self.store.get(first)["status"], "active")


class MaturationTests(unittest.TestCase):
    """Age-based STM→LTM maturation (Phase 2, memory-lifecycle-overhaul).

    The time-based promotion path: a short-term fact older than ``stm_max_age_days`` moves to
    LTM regardless of rehearsal/recall, so STM does not accumulate one-off facts forever.
    Recall-neutrality (STM vs matured-LTM score identically at the default weight) is already
    covered by ``test_stm.StmTierTests.test_recall_is_tier_agnostic_by_default`` — not
    duplicated here.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        # Maturation on at 1 day; every other consolidation lever off so we test it in isolation.
        self.cfg = replace(
            get_config(),
            distiller="heuristic",
            stm_max_age_days=1.0,
            stm_capacity=0,
            integrate_threshold=0,
            refine_keep_max=0,
            refine_prune_percentile=0,
            purge_horizon_days=0,
        )
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "test", "path": "/tmp/test", "label": "test"}

    def tearDown(self):
        self.store.close()
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def _add_aged(self, text: str, age_days: float) -> str:
        """Add an STM fact and back-date its capture time (and last_seen) by ``age_days``."""
        service.add_facts(self.store, self.embedder, self.cfg, self.project, "s1", [text])
        fid = self.store.fact_id(self.project["key"], text)
        stamp = NOW - age_days * 86400
        self.store.db.execute("UPDATE facts SET created_at = ?, last_seen = ? WHERE id = ?", (stamp, stamp, fid))
        self.store.db.commit()
        return fid

    def test_mature_transfers_aged_stm(self):
        old = self._add_aged("two-day-old fact", age_days=2)
        fresh = self._add_aged("half-day-old fact", age_days=0.5)
        matured = mature(self.store, self.cfg, self.project, now=NOW)
        self.assertEqual(matured, 1)
        self.assertEqual(self.store.get(old)["tier"], "ltm")
        self.assertEqual(self.store.get(fresh)["tier"], "stm")  # younger than the horizon

    def test_mature_preserves_last_seen(self):
        # Maturation is NOT a rehearsal — it must not refresh recency, or the forgetting curve
        # would treat a dormant fact as freshly seen.
        old = self._add_aged("aged fact", age_days=3)
        before = self.store.get(old)["last_seen"]
        mature(self.store, self.cfg, self.project, now=NOW)
        self.assertEqual(self.store.get(old)["last_seen"], before)

    def test_mature_disabled_is_noop(self):
        self._add_aged("aged fact", age_days=5)
        cfg = replace(self.cfg, stm_max_age_days=0)
        self.assertEqual(mature(self.store, cfg, self.project, now=NOW), 0)

    def test_mature_is_idempotent(self):
        self._add_aged("aged fact", age_days=2)
        self.assertEqual(mature(self.store, self.cfg, self.project, now=NOW), 1)
        self.assertEqual(mature(self.store, self.cfg, self.project, now=NOW), 0)  # already LTM

    def test_consolidate_matures_before_displacing(self):
        # An aged fact matures to LTM (via the mature stage) and so is exempt from STM
        # displacement even at capacity 0-with-overflow; a fresh fact stays STM.
        old = self._add_aged("aged fact", age_days=2)
        fresh = self._add_aged("fresh fact", age_days=0.1)
        cfg = replace(self.cfg, stm_capacity=1)  # only room for 1 STM fact
        counts = consolidate(self.store, cfg, self.project, now=NOW)
        self.assertEqual(counts["matured"], 1)
        self.assertEqual(counts["displaced"], 0)  # aged one left STM by maturation, fresh one fits
        self.assertEqual(self.store.get(old)["tier"], "ltm")
        self.assertEqual(self.store.get(old)["status"], "active")
        self.assertEqual(self.store.get(fresh)["tier"], "stm")


if __name__ == "__main__":
    unittest.main()
