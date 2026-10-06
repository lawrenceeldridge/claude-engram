"""Phase 1 (memory-lifecycle-overhaul): similarity-based supersession candidate selection.

The bug these guard against: the distiller could only be shown the most *recent* facts as
supersession candidates, so an OLD fact a change contradicts (e.g. a now-false "blocked"
note after an infrastructure change) was never a candidate and could never be retired by a
vocabulary-disjoint update. The fix ranks candidates by embedding *similarity* to the
session ("a highly correlated memory"), topping up with recency, so aged-but-relevant facts
are offered to the LLM.

All stdlib: `hash` embedding (lexical, so token overlap → high cosine), a stub distiller,
no network. Ages are seeded deterministically via ``bulk_add_records`` explicit timestamps
(``last_seen == created_at``), so the recency window is not wall-clock-flaky.

Run: python3 -m unittest discover -s plugins/engram/tests
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from _harness import temp_data_dir

from core import service
from core.config import get_config
from core.ports.distill import DistilledFact
from core.ports.embedding import HashEmbedding
from core.ports.scorer import get_scorer
from core.store import Store

_OLD = 1_000.0  # seeded age of the "aged" fact
_NEW = 2_000.0  # seeded age of the newer facts that fill the recency window


class _SupersedingDistiller:
    """Supersedes any candidate whose text contains ``marker`` — i.e. it can only retire a
    fact it was actually shown in ``existing``. That is the whole point: the fix must put the
    aged fact into ``existing`` for this to reach it."""

    def __init__(self, marker: str = "blocked") -> None:
        self.marker = marker
        self.seen_existing: list[tuple[str, str]] = []

    def distill(self, text: str, existing: list[tuple[str, str]]) -> list[DistilledFact]:
        self.seen_existing = existing
        victims = [fid for fid, ftext in existing if self.marker in ftext]
        return [DistilledFact(text="deployment now runs without vpn access", supersedes=victims)]


class _BrokenEmbedder(HashEmbedding):
    def embed_one(self, text: str) -> list[float]:  # type: ignore[override]
        raise RuntimeError("embedder down")


class SupersedeCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_data_dir(self)
        # LLM distiller so capture_text takes the similarity-candidate path; tiny budget so a
        # handful of seeded facts is enough to overflow the recency window.
        self.cfg = replace(get_config(), distiller="ollama", supersede_candidates=3, supersede_candidate_min_sim=0.3)
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.scorer = get_scorer(self.cfg)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}

    def tearDown(self):
        self.store.close()

    def _seed(self, records: list[tuple[str, float]]) -> None:
        service.bulk_add_records(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "seed",
            [(DistilledFact(text=t), ts) for t, ts in records],
        )

    def _id(self, text: str) -> str:
        return self.store.fact_id(self.project["key"], text)

    def test_similarity_reaches_aged_fact_beyond_recency(self):
        """The aged, topically-related fact is offered as a candidate even though the recency
        window (filled by newer, unrelated facts) excludes it."""
        aged = "deployment is blocked without vpn access to the cluster"
        self._seed([(aged, _OLD)])
        self._seed([(f"the frontend uses tailwind for styling variant {i}", _NEW + i) for i in range(5)])
        aged_id = self._id(aged)

        # Precondition (reproduces the bug): recency alone does NOT surface the aged fact.
        recent_ids = {r["id"] for r in self.store.recent(self.project["key"], self.cfg.supersede_candidates)}
        self.assertNotIn(aged_id, recent_ids)

        # The fix: similarity selection surfaces it.
        session = "deployment vpn access to the cluster changed"
        candidates = service._supersede_candidates(
            self.store, self.embedder, self.cfg, self.project["key"], session, self.scorer
        )
        cand_ids = {fid for fid, _ in candidates}
        self.assertIn(aged_id, cand_ids)
        self.assertLessEqual(len(candidates), self.cfg.supersede_candidates)

    def test_capture_supersedes_aged_fact(self):
        """End-to-end: capture_text offers the aged fact, the distiller supersedes it, and the
        store marks it superseded."""
        aged = "deployment is blocked without vpn access to the cluster"
        self._seed([(aged, _OLD)])
        self._seed([(f"unrelated styling note {i}", _NEW + i) for i in range(5)])
        aged_id = self._id(aged)

        stub = _SupersedingDistiller(marker="blocked")
        with mock.patch.object(service, "get_distiller", return_value=stub):
            service.capture_text(
                self.store,
                self.embedder,
                self.cfg,
                self.project,
                "s1",
                "deployment vpn access to the cluster is no longer required",
            )

        self.assertEqual(self.store.get(aged_id)["status"], "superseded")
        # And the stub really was shown the aged fact (not just retired by coincidence).
        self.assertIn(aged_id, {fid for fid, _ in stub.seen_existing})

    def test_unrelated_aged_fact_not_offered(self):
        """An aged fact unrelated to the session is reached by neither similarity (below the
        gate) nor recency (window filled by newer facts), so it is not offered."""
        aged = "the invoice pdf renders with a broken arabic font"
        self._seed([(aged, _OLD)])
        self._seed([(f"newer note about caching layer {i}", _NEW + i) for i in range(5)])
        aged_id = self._id(aged)

        session = "deployment vpn access to the cluster changed"
        candidates = service._supersede_candidates(
            self.store, self.embedder, self.cfg, self.project["key"], session, self.scorer
        )
        self.assertNotIn(aged_id, {fid for fid, _ in candidates})
        self.assertLessEqual(len(candidates), self.cfg.supersede_candidates)

    def test_candidates_failopen_to_recency(self):
        """If the similarity scan errors (e.g. a dead embedder), candidate selection degrades
        to recency-only rather than breaking capture."""
        self._seed([(f"note {i}", _NEW + i) for i in range(3)])
        candidates = service._supersede_candidates(
            self.store, _BrokenEmbedder(dim=self.cfg.dim), self.cfg, self.project["key"], "anything", self.scorer
        )
        # No exception, and we still get the recency window back.
        self.assertEqual(len(candidates), 3)


if __name__ == "__main__":
    unittest.main()
