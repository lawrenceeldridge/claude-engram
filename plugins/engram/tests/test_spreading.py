"""Idea #4 — associative spreading activation (ACT-R). Pure spread + entity extraction, the
edge store, and the capture gate. Off by default (spread_weight=0): no edges, hot path untouched."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from core import service
from core.config import get_config
from core.domain.entities import extract_entities
from core.domain.spreading import spread
from core.ports.embedding import HashEmbedding
from core.store import Store


class EntityExtractionTests(unittest.TestCase):
    def test_extracts_structured_tokens(self):
        ents = extract_entities("The recall path lives in core/recall/__init__.py and calls add_records.")
        self.assertIn("core/recall/__init__.py", ents)
        self.assertIn("add_records", ents)

    def test_prose_has_no_false_entities(self):
        self.assertEqual(extract_entities("the quick brown fox jumps over the lazy dog"), set())

    def test_camelcase_and_empty(self):
        self.assertIn("retentionweights", extract_entities("tune the RetentionWeights please"))
        self.assertEqual(extract_entities(""), set())


class SpreadTests(unittest.TestCase):
    def test_boosts_linked_candidates(self):
        boosts = spread(["a", "b", "c"], [("a", "b", 1.0)], weight=0.5)
        self.assertAlmostEqual(boosts["a"], 0.5)
        self.assertAlmostEqual(boosts["b"], 0.5)
        self.assertNotIn("c", boosts)

    def test_weight_zero_is_empty(self):
        self.assertEqual(spread(["a", "b"], [("a", "b", 1.0)], weight=0.0), {})

    def test_edge_to_non_candidate_ignored(self):
        self.assertEqual(spread(["a", "b"], [("a", "z", 1.0)], weight=1.0), {})


class EdgeStoreTests(unittest.TestCase):
    def _store(self):
        tmp = tempfile.mkdtemp(prefix="engram-test-edge-")
        return Store(Path(tmp) / "m.db")

    def test_edges_table_exists(self):
        store = self._store()
        names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        store.close()
        self.assertIn("fact_edges", names)

    def test_add_edges_and_neighbours_accumulate(self):
        store = self._store()
        store.add_edges([("a", "b", "cooc", 1.0)])
        store.add_edges([("a", "b", "cooc", 1.0)])  # repeat strengthens
        nbrs = store.neighbours(["a"])
        store.close()
        self.assertEqual(len(nbrs), 1)
        self.assertEqual(nbrs[0], ("a", "b", 2.0))


class CaptureGateTests(unittest.TestCase):
    def _run(self, spread_weight: float) -> int:
        base = get_config()
        cfg = replace(base, spread_weight=spread_weight)
        embedder = HashEmbedding(dim=cfg.dim)
        tmp = tempfile.mkdtemp(prefix="engram-test-cap-")
        store = Store(Path(tmp) / "m.db")
        project = {"key": "cap", "path": tmp, "label": "cap"}
        service.add_facts(
            store, embedder, cfg, project, "s", ["deploy uses core/deploy.py", "deploy runs core/deploy.py nightly"]
        )
        count = store.db.execute("SELECT COUNT(*) FROM fact_edges").fetchone()[0]
        store.close()
        return count

    def test_off_by_default_records_no_edges(self):
        self.assertEqual(self._run(0.0), 0)

    def test_enabled_records_edges(self):
        self.assertGreater(self._run(1.0), 0)


if __name__ == "__main__":
    unittest.main()
