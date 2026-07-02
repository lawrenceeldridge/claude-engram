"""Recall API + MCP server tests — stdlib unittest, no external deps.

Covers the 0.4.0 memory-first surface: calibrated confidence, the confidence-gated
structured recall verdict, and the pure JSON-RPC dispatch of the MCP stdio server.

Run: python3 -m unittest discover -s plugins/ltm/tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

from core.confidence import compute_confidence  # noqa: E402
from core.config import get_config  # noqa: E402
from core.embedding import HashEmbedding  # noqa: E402
from core.lexical import has_overlap, tokenize  # noqa: E402
from core.store import Store  # noqa: E402
from core import service  # noqa: E402


class ConfidenceTests(unittest.TestCase):
    def test_empty_scores_zero(self):
        self.assertEqual(compute_confidence([])["confidence"], 0.0)

    def test_dominant_top_with_identity_is_high(self):
        strong = compute_confidence([1.2, 0.1], has_identity_match=True)["confidence"]
        self.assertGreater(strong, 0.6)

    def test_tied_top_lowers_confidence(self):
        tied = compute_confidence([0.5, 0.49], has_identity_match=True)["confidence"]
        clear = compute_confidence([0.5, 0.05], has_identity_match=True)["confidence"]
        self.assertLess(tied, clear)

    def test_identity_miss_penalised(self):
        hit = compute_confidence([0.8, 0.1], has_identity_match=True)["confidence"]
        miss = compute_confidence([0.8, 0.1], has_identity_match=False)["confidence"]
        self.assertLess(miss, hit)


class LexicalTests(unittest.TestCase):
    def test_stopwords_and_short_tokens_dropped(self):
        self.assertNotIn("the", tokenize("the deployment is on it"))
        self.assertIn("deployment", tokenize("the deployment is on it"))

    def test_overlap_detects_shared_content_token(self):
        self.assertTrue(has_overlap("how does deployment work", "deployment runs on github actions"))
        self.assertFalse(has_overlap("database schema", "frontend styling tokens"))


class RecallStructuredTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LTM_DATA_DIR"] = self.tmp.name
        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "test", "path": "/tmp/test", "label": "test"}

    def tearDown(self):
        self.store.close()
        os.environ.pop("LTM_DATA_DIR", None)
        self.tmp.cleanup()

    def test_empty_store_returns_no_memory(self):
        result = service.recall_structured(self.store, self.embedder, self.cfg, self.project, "anything")
        self.assertEqual(result["verdict"], "no_memory")
        self.assertEqual(result["confidence"], 0.0)
        self.assertEqual(result["facts"], [])
        self.assertIn("do not assume prior context", result["guidance"])

    def test_dim_divergence_returns_embedding_mismatch(self):
        service.add_facts(
            self.store, self.embedder, self.cfg, self.project, "s1",
            ["The deployment pipeline runs on github actions."],
        )
        other_space = HashEmbedding(dim=self.cfg.dim // 2)
        result = service.recall_structured(
            self.store, other_space, self.cfg, self.project, "deployment pipeline"
        )
        self.assertEqual(result["verdict"], "embedding_mismatch")
        self.assertEqual(result["facts"], [])
        self.assertIn("configuration problem", result["guidance"])

    def test_relevant_recall_is_ok(self):
        service.add_facts(
            self.store, self.embedder, self.cfg, self.project, "s1",
            ["The deployment pipeline runs on github actions with a manual approval gate."],
        )
        result = service.recall_structured(
            self.store, self.embedder, self.cfg, self.project, "how does the deployment pipeline work"
        )
        self.assertEqual(result["verdict"], "ok")
        self.assertGreaterEqual(result["confidence"], self.cfg.recall_min_confidence)
        self.assertTrue(any("github actions" in f["text"] for f in result["facts"]))

    def test_budget_packs_and_reports_dropped(self):
        facts = [f"fact number {i} about compact memory storage systems and budgets" for i in range(20)]
        service.add_facts(self.store, self.embedder, self.cfg, self.project, "s1", facts)
        from dataclasses import replace

        cfg = replace(self.cfg, recall_max_chars=120)
        result = service.recall_structured(
            self.store, self.embedder, cfg, self.project, "compact memory storage", k=20
        )
        used = sum(len(f["text"]) for f in result["facts"])
        self.assertLessEqual(used - len(result["facts"][0]["text"]), 120)
        self.assertEqual(result["dropped"], result["matched"] - result["returned"])


class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LTM_DATA_DIR"] = self.tmp.name
        import mcp_server

        self.mcp = mcp_server
        # fresh engine per test so the cached store points at this temp dir
        self.mcp.ENGINE = mcp_server._Engine()

    def tearDown(self):
        os.environ.pop("LTM_DATA_DIR", None)
        self.tmp.cleanup()

    def test_initialize_echoes_protocol_and_advertises_tools(self):
        resp = self.mcp._handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        )
        self.assertEqual(resp["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", resp["result"]["capabilities"])

        listed = self.mcp._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in listed["result"]["tools"]}
        self.assertEqual(names, {"recall", "list_projects"})

    def test_notification_gets_no_response(self):
        self.assertIsNone(self.mcp._handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_unknown_method_is_method_not_found(self):
        resp = self.mcp._handle({"jsonrpc": "2.0", "id": 9, "method": "does/not/exist"})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_tools_call_recall_returns_wrapped_json(self):
        resp = self.mcp._handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "recall", "arguments": {"query": "anything at all"}},
            }
        )
        text = resp["result"]["content"][0]["text"]
        payload = json.loads(text)
        self.assertIn("verdict", payload)
        self.assertIn("confidence", payload)

    def test_tools_call_list_projects(self):
        resp = self.mcp._handle(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "list_projects"}}
        )
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("projects", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
