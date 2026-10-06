"""Phase 5 (memory-lifecycle-overhaul): the forgetting curve — fades unless recalled.

`refine_min_retention` is an absolute retention floor: a fact is pruned once its retention
score has decayed below it. Because retention composes recency decay with recall (`use`),
reinforcement (`frequency`) and importance (`salience`, from Phase 4), a dormant fact fades
over time UNLESS it has been recalled, reinforced, or is important. Reversible
(`status='pruned'`); anti-pattern exemption is covered by
`test_antipatterns.LifecycleTests.test_refine_exempts_antipatterns` (the guard is
prune-mode-agnostic), so it is not duplicated here.

Ages/recalls are seeded deterministically (explicit timestamps) so dormancy is not
wall-clock-flaky. Stdlib: hash embedding, heuristic distiller, no network.

Run: python3 -m unittest discover -s plugins/engram/tests
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from _harness import temp_data_dir

from core import service
from core.config import get_config
from core.consolidation.refine import refine
from core.ports.distill import DistilledFact
from core.ports.embedding import HashEmbedding
from core.store import Store

NOW = 2_000_000_000.0  # far enough ahead that a 400-day back-date is still a positive stamp
DORMANT = NOW - 400 * 86400  # ~13 half-lives at half_life_days=30 → recency term ≈ 0


class ForgettingCurveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_data_dir(self)
        # Only the forgetting floor is on; every other consolidation lever off, so we test the
        # floor in isolation. Floor sits between a dormant plain discovery and a protected fact.
        self.cfg = replace(
            get_config(),
            embedding="hash",
            distiller="heuristic",
            stm_capacity=0,
            stm_max_age_days=0,
            integrate_threshold=0,
            refine_keep_max=0,
            refine_prune_percentile=0,
            refine_min_retention=0.15,
            purge_horizon_days=0,
        )
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}

    def tearDown(self):
        self.store.close()

    def _seed(self, text: str, *, type_: str = "discovery", recalls: int = 0, freq: int = 1) -> str:
        service.add_records(
            self.store, self.embedder, self.cfg, self.project, "s1", [DistilledFact(text=text, type=type_)]
        )
        fid = self.store.fact_id(self.project["key"], text)
        # Reinforce/recall at the dormant timestamp so those signals rise WITHOUT refreshing
        # recency (the whole point: an old fact kept alive by recall/reinforcement, not recency).
        for _ in range(max(0, freq - 1)):
            self.store.reinforce(fid, DORMANT)
        for _ in range(recalls):
            self.store.mark_recalled([fid], now=DORMANT)
        self.store.db.execute("UPDATE facts SET created_at = ?, last_seen = ? WHERE id = ?", (DORMANT, DORMANT, fid))
        self.store.db.commit()
        return fid

    def _status(self, fid: str) -> str:
        return self.store.get(fid)["status"]

    def test_dormant_low_salience_fades_importance_survives(self):
        stale = self._seed("a passing discovery nobody revisits", type_="discovery")
        important = self._seed("chose postgres over mysql for strong typing", type_="decision")
        pruned = refine(self.store, self.cfg, self.project, now=NOW)
        self.assertEqual(pruned, 1)
        self.assertEqual(self._status(stale), "pruned")  # faded — dormant, unrecalled, low salience
        self.assertEqual(self._status(important), "active")  # importance keeps it above the floor

    def test_recall_and_reinforcement_protect_from_fade(self):
        stale = self._seed("dormant unrecalled note", type_="discovery")
        recalled = self._seed("dormant but recalled note", type_="discovery", recalls=7)
        reinforced = self._seed("dormant but reinforced note", type_="discovery", freq=8)
        refine(self.store, self.cfg, self.project, now=NOW)
        self.assertEqual(self._status(stale), "pruned")
        self.assertEqual(self._status(recalled), "active")  # retrieval kept it (use term)
        self.assertEqual(self._status(reinforced), "active")  # reinforcement kept it (frequency term)

    def test_fade_is_reversible(self):
        stale = self._seed("dormant note", type_="discovery")
        refine(self.store, self.cfg, self.project, now=NOW)
        row = self.store.get(stale)
        self.assertIsNotNone(row)  # archived, not deleted
        self.assertEqual(row["status"], "pruned")

    def test_floor_off_by_default_is_noop(self):
        stale = self._seed("dormant note", type_="discovery")
        cfg = replace(self.cfg, refine_min_retention=0)
        self.assertEqual(refine(self.store, cfg, self.project, now=NOW), 0)
        self.assertEqual(self._status(stale), "active")


if __name__ == "__main__":
    unittest.main()
