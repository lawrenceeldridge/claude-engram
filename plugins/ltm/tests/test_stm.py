"""STM/LTM tier + retrieval-attribution tests (Phase 2 of stm-ltm-membus).

Stdlib unittest, hash embedder, no network. Verifies the Atkinson-Shiffrin tier
lifecycle (fresh->STM, rehearsal promotes to LTM), displacement, per-fact recall
attribution, and that recall stays tier-agnostic by default (behaviour parity).
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
from core.embedding import HashEmbedding  # noqa: E402
from core.recall import search  # noqa: E402
from core.store import Store  # noqa: E402


class StmTierTests(unittest.TestCase):
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

    def _add(self, text: str, cfg=None) -> int:
        return service.add_facts(self.store, self.embedder, cfg or self.cfg, self.project, "s1", [text])

    def _fid(self, text: str) -> str:
        return self.store.fact_id(self.project["key"], text)

    def _set_tier(self, fid: str, tier: str) -> None:
        # Flip tier without touching recency, to isolate the tier effect in scoring.
        self.store.db.execute("UPDATE facts SET tier = ? WHERE id = ?", (tier, fid))
        self.store.db.commit()

    # --- schema / migration ---

    def test_migration_added_stm_columns(self):
        cols = {row[1] for row in self.store.db.execute("PRAGMA table_info(facts)")}
        self.assertIn("tier", cols)
        self.assertIn("recall_count", cols)
        self.assertIn("last_recalled", cols)

    # --- fresh capture is STM ---

    def test_fresh_capture_lands_in_stm(self):
        self._add("the deploy target is fly.io")
        self.assertEqual(self.store.get(self._fid("the deploy target is fly.io"))["tier"], "stm")

    # --- reinforce returns the new frequency ---

    def test_reinforce_returns_new_frequency(self):
        self._add("the ci runs on github actions")
        fid = self._fid("the ci runs on github actions")
        self.assertEqual(self.store.reinforce(fid), 2)
        self.assertEqual(self.store.reinforce(fid), 3)
        self.assertEqual(self.store.reinforce("nonexistent-id"), 0)

    # --- promotion on rehearsal ---

    def test_rehearsal_promotes_stm_to_ltm(self):
        text = "the primary key is a content hash"
        self._add(text)  # freq 1 -> stm
        fid = self._fid(text)
        self.assertEqual(self.store.get(fid)["tier"], "stm")
        self._add(text)  # re-capture -> reinforce freq 2 >= promote_after_freq(2) -> ltm
        self.assertEqual(self.store.get(fid)["tier"], "ltm")

    def test_promote_after_freq_is_configurable(self):
        cfg = replace(self.cfg, promote_after_freq=3)
        text = "distiller falls back to heuristic"
        self._add(text, cfg)
        fid = self._fid(text)
        self._add(text, cfg)  # freq 2 < 3 -> still stm
        self.assertEqual(self.store.get(fid)["tier"], "stm")
        self._add(text, cfg)  # freq 3 -> ltm
        self.assertEqual(self.store.get(fid)["tier"], "ltm")

    def test_promote_only_affects_stm_and_is_idempotent(self):
        text = "recall is a cosine scan"
        self._add(text)
        fid = self._fid(text)
        self.store.promote(fid)
        self.assertEqual(self.store.get(fid)["tier"], "ltm")
        self.store.promote(fid)  # already ltm — no error, still ltm
        self.assertEqual(self.store.get(fid)["tier"], "ltm")

    def test_stm_rows_returns_only_active_stm(self):
        self._add("alpha fact")
        self._add("beta fact")
        self.store.promote(self._fid("beta fact"))  # beta -> ltm
        texts = {r["text"] for r in self.store.stm_rows(self.project["key"])}
        self.assertEqual(texts, {"alpha fact"})

    # --- displacement (opt-in, reversible) ---

    def test_displace_stm_disabled_by_default(self):
        for i in range(5):
            self._add(f"fact number {i}")
        self.assertEqual(self.store.displace_stm(self.project["key"], 0), 0)
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 5)

    def test_displace_stm_archives_weakest_beyond_capacity(self):
        for i in range(5):
            self._add(f"fact number {i}")
        archived = self.store.displace_stm(self.project["key"], 3)
        self.assertEqual(archived, 2)
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 3)
        # Reversible archive, not a delete: rows still exist, just not 'active'.
        total = self.store.db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.assertEqual(total, 5)

    # --- retrieval attribution ---

    def test_mark_recalled_increments_and_stamps(self):
        text = "vectors are int8 quantised"
        self._add(text)
        fid = self._fid(text)
        self.assertEqual(self.store.get(fid)["recall_count"], 0)
        self.assertIsNone(self.store.get(fid)["last_recalled"])
        self.assertEqual(self.store.mark_recalled([fid]), 1)
        row = self.store.get(fid)
        self.assertEqual(row["recall_count"], 1)
        self.assertIsNotNone(row["last_recalled"])
        self.assertEqual(self.store.mark_recalled([]), 0)  # empty is a no-op

    def test_recall_structured_records_attribution(self):
        self._add("the memory viewer runs on localhost")
        result = service.recall_structured(
            self.store, self.embedder, self.cfg, self.project, "memory viewer localhost runs"
        )
        self.assertGreaterEqual(result["returned"], 1)  # sanity: it came back
        fid = self._fid("the memory viewer runs on localhost")
        self.assertGreaterEqual(self.store.get(fid)["recall_count"], 1)

    # --- recall parity: tier-agnostic by default ---

    def test_recall_is_tier_agnostic_by_default(self):
        text = "the config lives in pyproject"
        self._add(text)
        fid = self._fid(text)
        score_stm = self._score_of(fid, "config pyproject")
        self._set_tier(fid, "ltm")
        score_ltm = self._score_of(fid, "config pyproject")
        self.assertAlmostEqual(score_stm, score_ltm, places=6)

    def test_stm_recall_weight_penalizes_stm(self):
        cfg = replace(self.cfg, stm_recall_weight=0.5)
        text = "the daemon holds the model warm"
        self._add(text)
        fid = self._fid(text)
        score_stm = self._score_of(fid, "daemon model warm", cfg)
        self._set_tier(fid, "ltm")
        score_ltm = self._score_of(fid, "daemon model warm", cfg)
        self.assertAlmostEqual(score_stm, score_ltm * 0.5, places=6)

    def _score_of(self, fid: str, query: str, cfg=None) -> float:
        hits = search(self.store, self.embedder, self.project, query, cfg or self.cfg, min_sim=-1.0)
        return next(score for score, row in hits if row["id"] == fid)


if __name__ == "__main__":
    unittest.main()
