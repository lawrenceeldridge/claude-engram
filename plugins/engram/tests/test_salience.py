"""Phase 4 (memory-lifecycle-overhaul): salience — importance from observation type.

Wires the retention score's previously-inert salience term to a real signal: a fact's
observation type (a decision/bugfix is more worth keeping than a passing discovery). This
feeds the sleep-pass forgetting curve (retention) ONLY — the recall Priority Score is
untouched, so recall ordering is unchanged. Replaces the dead ``len/240`` importance
heuristic that was stored at capture but read nowhere.

Stdlib: hash embedding, no network.

Run: python3 -m unittest discover -s plugins/engram/tests
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from _harness import temp_data_dir

from core import service
from core.config import get_config
from core.consolidation.scoring import RetentionFeatures, features_from_row, retention
from core.domain.scoring import _DEFAULT_SALIENCE, salience_of
from core.ports.distill import DistilledFact
from core.ports.embedding import HashEmbedding
from core.store import Store

NOW = 1_000_000.0
HL = 30.0


class SalienceOfTests(unittest.TestCase):
    def test_ordering_by_type(self):
        self.assertEqual(salience_of("decision"), 1.0)
        self.assertEqual(salience_of("bugfix"), 1.0)
        self.assertGreater(salience_of("decision"), salience_of("refactor"))
        self.assertGreater(salience_of("refactor"), salience_of("discovery"))

    def test_case_insensitive(self):
        self.assertEqual(salience_of("DECISION"), 1.0)
        self.assertEqual(salience_of("  Bugfix  "), 1.0)

    def test_unknown_and_empty_fall_to_default(self):
        self.assertEqual(salience_of(""), _DEFAULT_SALIENCE)
        self.assertEqual(salience_of("nonsense-type"), _DEFAULT_SALIENCE)
        self.assertEqual(salience_of(None), _DEFAULT_SALIENCE)  # type: ignore[arg-type]


class RetentionSalienceTests(unittest.TestCase):
    """The salience term is now live in the retention score (was inert at 0)."""

    def test_higher_salience_retains_higher(self):
        base = RetentionFeatures(frequency=1, recall_count=0, last_seen=NOW)
        decision = retention(replace(base, salience=salience_of("decision")), NOW, HL)
        discovery = retention(replace(base, salience=salience_of("discovery")), NOW, HL)
        self.assertGreater(decision, discovery)


class SalienceWiringTests(unittest.TestCase):
    """Capture stores type-based salience in `importance`, and features_from_row reads type."""

    def setUp(self):
        self.tmp = temp_data_dir(self)
        self.cfg = replace(get_config(), embedding="hash")
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}

    def tearDown(self):
        self.store.close()

    def _add_typed(self, text: str, type_: str) -> str:
        service.add_records(
            self.store, self.embedder, self.cfg, self.project, "s1", [DistilledFact(text=text, type=type_)]
        )
        return self.store.fact_id(self.project["key"], text)

    def test_capture_stores_type_salience_not_length(self):
        decision = self._add_typed("chose postgres over mysql for strong typing", "decision")
        discovery = self._add_typed("noticed the cache is warm on the second call", "discovery")
        self.assertEqual(self.store.get(decision)["importance"], 1.0)
        self.assertEqual(self.store.get(discovery)["importance"], salience_of("discovery"))

    def test_features_from_row_salience_follows_type(self):
        decision = self._add_typed("adopt ruff for linting and formatting", "decision")
        discovery = self._add_typed("the build script lives in justfile", "discovery")
        self.assertEqual(features_from_row(self.store.get(decision)).salience, 1.0)
        self.assertEqual(features_from_row(self.store.get(discovery)).salience, salience_of("discovery"))

    def test_retention_rewards_salient_type_end_to_end(self):
        # Two facts identical but for type → only salience differs → decision retains higher.
        decision = self._add_typed("decision alpha", "decision")
        discovery = self._add_typed("discovery alpha", "discovery")
        rd = retention(features_from_row(self.store.get(decision)), NOW, HL)
        rx = retention(features_from_row(self.store.get(discovery)), NOW, HL)
        self.assertGreater(rd, rx)


if __name__ == "__main__":
    unittest.main()
