"""0.5.0 quality-layer tests — rank fusion, drift canary, recall ledger, MCP cache.

Run: python3 -m unittest discover -s plugins/engram/tests
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

from core import service  # noqa: E402
from core.config import get_config  # noqa: E402
from core.domain.fusion import Channel, fuse  # noqa: E402
from core.index.drift import check, pin  # noqa: E402
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.recall import search_fused  # noqa: E402
from core.store import Store  # noqa: E402


class FusionTests(unittest.TestCase):
    def test_agreement_across_channels_wins(self):
        channels = [
            Channel("similarity", ["a", "b", "c"]),
            Channel("lexical", ["a", "c", "b"]),
            Channel("recency", ["b", "a", "c"]),
        ]
        ranked = fuse(channels)
        self.assertEqual(ranked[0].fact_id, "a")

    def test_higher_weighted_channel_breaks_ties(self):
        # 'x' leads only the top-weighted similarity channel; 'y' leads only recency.
        channels = [Channel("similarity", ["x", "y"]), Channel("recency", ["y", "x"])]
        ranked = fuse(channels)
        self.assertEqual(ranked[0].fact_id, "x")

    def test_empty_channels_yield_nothing(self):
        self.assertEqual(fuse([]), [])


class SearchFusedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "test", "path": "/tmp/test", "label": "test"}

    def tearDown(self):
        self.store.close()
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def test_lexical_overlap_rescues_below_similarity_gate(self):
        service.add_facts(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s1",
            ["the orbital telescope calibration sequence completed"],
        )
        # A high min_sim would gate a weak embedding out; lexical overlap keeps it.
        hits = search_fused(
            self.store, self.embedder, self.project, "telescope calibration", self.cfg, k=5, min_sim=0.99
        )
        self.assertTrue(any("telescope calibration" in row["text"] for _s, _sim, row in hits))

    def test_returns_similarity_per_hit(self):
        service.add_facts(self.store, self.embedder, self.cfg, self.project, "s1", ["github actions deploy pipeline"])
        hits = search_fused(self.store, self.embedder, self.project, "deploy pipeline", self.cfg)
        self.assertTrue(hits)
        _score, sim, _row = hits[0]
        self.assertGreaterEqual(sim, 0.0)


class DriftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.embedder = HashEmbedding(dim=128)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unpinned_then_pinned_is_stable(self):
        self.assertEqual(check(self.embedder, self.dir, "hash:default:128")["status"], "unpinned")
        pin(self.embedder, self.dir, "hash:default:128")
        result = check(self.embedder, self.dir, "hash:default:128")
        self.assertEqual(result["status"], "ok")
        self.assertGreater(result["mean_similarity"], 0.99)

    def test_model_change_is_flagged(self):
        pin(self.embedder, self.dir, "hash:default:128")
        result = check(self.embedder, self.dir, "fastembed:bge:128")
        self.assertEqual(result["status"], "model_changed")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "test", "path": "/tmp/test", "label": "test"}

    def tearDown(self):
        self.store.close()
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def test_recall_is_logged_to_ledger(self):
        service.add_facts(
            self.store, self.embedder, self.cfg, self.project, "s1", ["deployment runs on github actions"]
        )
        service.recall_structured(self.store, self.embedder, self.cfg, self.project, "deployment")
        service.recall_structured(self.store, self.embedder, self.cfg, self.project, "nothing relevant here xyzzy")
        stats = self.store.recall_stats(self.project["key"])
        self.assertEqual(stats["total"], 2)
        self.assertEqual(sum(stats["by_verdict"].values()), 2)


class McpCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        import mcp_server

        self.mcp = mcp_server
        self.mcp.ENGINE = mcp_server._Engine()

    def tearDown(self):
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def test_repeated_recall_is_served_from_cache(self):
        args = {"query": "anything at all", "project": None}
        first = self.mcp.ENGINE.recall(args)
        second = self.mcp.ENGINE.recall(args)
        self.assertEqual(first, second)
        # cache hit must not re-log — exactly one ledger row for the two calls
        self.assertEqual(self.mcp.ENGINE.store.recall_stats()["total"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
