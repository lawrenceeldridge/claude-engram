"""Phase 3 (memory-lifecycle-overhaul): distiller-assisted memory review & invalidation.

Covers the pure review parser, the Store-backed invalidation Command, the review/apply
service functions, the recall id exposure that lets a caller target a fact, and the MCP
curation tools (invalidate_memory / review_memory). Stdlib: hash embedding, a stub distiller,
no network.

Run: python3 -m unittest discover -s plugins/engram/tests
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest import mock

from _harness import temp_data_dir

from core import service
from core.config import get_config
from core.ports.distill import HeuristicDistiller, parse_review
from core.ports.embedding import HashEmbedding
from core.store import Store


class _ReviewStub:
    """Distiller stub: returns pre-set review proposals and records what it was shown."""

    def __init__(self, proposals: list[dict]) -> None:
        self.proposals = proposals
        self.seen_ids: list[str] = []

    def review(self, facts: list[tuple[str, str]], context: str = "") -> list[dict]:
        self.seen_ids = [fid for fid, _ in facts]
        return [dict(p) for p in self.proposals]


class ParseReviewTests(unittest.TestCase):
    """Pure parser — keeps only well-formed, actionable entries naming a real id."""

    def test_filters_to_actionable_entries(self):
        valid = {"a1", "b2"}
        out = json.dumps(
            {
                "reviews": [
                    {"id": "a1", "verdict": "delete", "reason": "stale"},
                    {"id": "b2", "verdict": "update", "reason": "changed", "replacement": "new text"},
                    {"id": "a1", "verdict": "keep", "reason": "fine"},  # keep → dropped
                    {"id": "zz", "verdict": "delete", "reason": "unknown id"},  # unknown id → dropped
                    {"id": "b2", "verdict": "bogus"},  # bad verdict → dropped
                    {"id": "a1", "verdict": "update", "replacement": ""},  # no replacement → dropped
                ]
            }
        )
        props = parse_review(out, valid)
        self.assertEqual(len(props), 2)
        by_id = {p["id"]: p for p in props}
        self.assertEqual(by_id["a1"]["verdict"], "delete")
        self.assertEqual(by_id["b2"]["verdict"], "update")
        self.assertEqual(by_id["b2"]["replacement"], "new text")

    def test_empty_and_malformed(self):
        self.assertEqual(parse_review("not json", {"a"}), [])
        self.assertEqual(parse_review(json.dumps({"reviews": []}), {"a"}), [])

    def test_heuristic_distiller_reviews_nothing(self):
        self.assertEqual(HeuristicDistiller().review([("a", "some fact")]), [])


class _ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_data_dir(self)
        self.cfg = replace(get_config(), embedding="hash", distiller="ollama", review_enabled=True)
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}

    def tearDown(self):
        self.store.close()

    def _add(self, text: str) -> str:
        service.add_facts(self.store, self.embedder, self.cfg, self.project, "s1", [text])
        return self.store.fact_id(self.project["key"], text)


class InvalidateFactsTests(_ServiceCase):
    def test_retire_marks_expired_reversibly(self):
        fid = self._add("deployment is blocked without vpn")
        self.assertEqual(service.invalidate_facts(self.store, self.project["key"], [fid]), 1)
        row = self.store.get(fid)
        self.assertIsNotNone(row)  # reversible — the row still exists
        self.assertEqual(row["status"], "expired")

    def test_foreign_project_id_ignored(self):
        fid = self._add("a fact")
        self.assertEqual(service.invalidate_facts(self.store, "other-key", [fid]), 0)
        self.assertEqual(self.store.get(fid)["status"], "active")

    def test_deduplicates_ids(self):
        fid = self._add("a fact")
        self.assertEqual(service.invalidate_facts(self.store, self.project["key"], [fid, fid]), 1)


class ApplyReviewTests(_ServiceCase):
    def test_delete_update_and_keep(self):
        a = self._add("old blocked fact")
        b = self._add("stale fact to rewrite")
        keep = self._add("still valid fact")
        decisions = [
            {"id": a, "verdict": "delete", "replacement": ""},
            {"id": b, "verdict": "update", "replacement": "the corrected fact"},
            {"id": keep, "verdict": "keep", "replacement": ""},
        ]
        res = service.apply_review(self.store, self.embedder, self.cfg, self.project, decisions)
        self.assertEqual(res, {"deleted": 1, "updated": 1})
        self.assertEqual(self.store.get(a)["status"], "expired")
        self.assertEqual(self.store.get(b)["status"], "superseded")  # retired by its replacement
        new_id = self.store.fact_id(self.project["key"], "the corrected fact")
        self.assertEqual(self.store.get(new_id)["status"], "active")
        self.assertEqual(self.store.get(keep)["status"], "active")


class ReviewMemoriesTests(_ServiceCase):
    def test_proposals_carry_current_text(self):
        a = self._add("deploy needs vpn access")
        stub = _ReviewStub([{"id": a, "verdict": "delete", "reason": "infra changed", "replacement": ""}])
        with mock.patch.object(service, "get_distiller", return_value=stub):
            res = service.review_memories(self.store, self.embedder, self.cfg, self.project)
        self.assertEqual(len(res["proposals"]), 1)
        self.assertEqual(res["proposals"][0]["id"], a)
        self.assertEqual(res["proposals"][0]["text"], "deploy needs vpn access")

    def test_disabled_returns_empty(self):
        self._add("a fact")
        cfg = replace(self.cfg, review_enabled=False)
        stub = _ReviewStub([{"id": "x", "verdict": "delete"}])
        with mock.patch.object(service, "get_distiller", return_value=stub):
            res = service.review_memories(self.store, self.embedder, cfg, self.project)
        self.assertEqual(res["proposals"], [])
        self.assertIn("disabled", res["guidance"])

    def test_query_focuses_candidates_by_similarity(self):
        target = self._add("alpha widget subsystem behaviour")
        self._add("completely unrelated beta gamma note")
        stub = _ReviewStub([])
        with mock.patch.object(service, "get_distiller", return_value=stub):
            service.review_memories(self.store, self.embedder, self.cfg, self.project, query="alpha widget subsystem")
        self.assertIn(target, stub.seen_ids)  # the similar fact was offered to the reviewer


class RecallIdExposureTests(_ServiceCase):
    def test_recall_exposes_fact_id(self):
        fid = self._add("indexed fact about widgets and gadgets")
        res = service.recall_structured(self.store, self.embedder, self.cfg, self.project, "widgets gadgets")
        self.assertTrue(res["facts"], "expected at least one recalled fact")
        for fact in res["facts"]:
            self.assertIn("id", fact)
        self.assertEqual(res["facts"][0]["id"], fid)


class McpCurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_data_dir(self)
        import mcp_server

        self.mcp = mcp_server
        self.mcp.ENGINE = mcp_server._Engine()
        self.mcp.ENGINE._init()
        # Seed through the engine's own store/embedder/project so ids and project scope line up.
        self.project = self.mcp.ENGINE._project(None)
        self.store = self.mcp.ENGINE.store
        self.embedder = self.mcp.ENGINE.embedder
        self.cfg = self.mcp.ENGINE.cfg

    def _add(self, text: str) -> str:
        service.add_facts(self.store, self.embedder, self.cfg, self.project, "s1", [text])
        return self.store.fact_id(self.project["key"], text)

    def _call(self, name: str, arguments: dict) -> dict:
        resp = self.mcp._handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )
        return json.loads(resp["result"]["content"][0]["text"])

    def test_invalidate_memory_delete_retires_fact(self):
        fid = self._add("a stale blocked note")
        payload = self._call("invalidate_memory", {"ids": [fid], "mode": "delete"})
        self.assertEqual(payload["deleted"], 1)
        self.assertEqual(self.store.get(fid)["status"], "expired")

    def test_invalidate_memory_update_requires_replacement(self):
        fid = self._add("a fact")
        payload = self._call("invalidate_memory", {"ids": [fid], "mode": "update"})
        self.assertIn("error", payload)
        self.assertEqual(self.store.get(fid)["status"], "active")

    def test_review_memory_returns_proposals(self):
        fid = self._add("deploy needs vpn access")
        stub = _ReviewStub([{"id": fid, "verdict": "delete", "reason": "infra changed", "replacement": ""}])
        with mock.patch.object(service, "get_distiller", return_value=stub):
            payload = self._call("review_memory", {})
        self.assertEqual(len(payload["proposals"]), 1)
        self.assertEqual(payload["proposals"][0]["id"], fid)


if __name__ == "__main__":
    unittest.main()
