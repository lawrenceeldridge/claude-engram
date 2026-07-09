"""Recall API + MCP server tests — stdlib unittest, no external deps.

Covers the 0.4.0 memory-first surface: calibrated confidence, the confidence-gated
structured recall verdict, and the pure JSON-RPC dispatch of the MCP stdio server.

Run: python3 -m unittest discover -s plugins/engram/tests
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

from core import service  # noqa: E402
from core.config import get_config  # noqa: E402
from core.domain.confidence import compute_confidence  # noqa: E402
from core.domain.lexical import has_overlap, tokenize  # noqa: E402
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.store import Store  # noqa: E402


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
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "test", "path": "/tmp/test", "label": "test"}

    def tearDown(self):
        self.store.close()
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def test_empty_store_returns_no_memory(self):
        result = service.recall_structured(self.store, self.embedder, self.cfg, self.project, "anything")
        self.assertEqual(result["verdict"], "no_memory")
        self.assertEqual(result["confidence"], 0.0)
        self.assertEqual(result["facts"], [])
        self.assertIn("do not assume prior context", result["guidance"])

    def test_rows_for_project_paginates_newest_first(self):
        for i in range(5):
            service.add_facts(self.store, self.embedder, self.cfg, self.project, f"s{i}", [f"fact {i}"])
        all_rows = [r["text"] for r in self.store.rows_for_project(self.project["key"])]
        self.assertEqual(all_rows, [f"fact {i}" for i in reversed(range(5))])
        page1 = [r["text"] for r in self.store.rows_for_project(self.project["key"], limit=2, offset=0)]
        page2 = [r["text"] for r in self.store.rows_for_project(self.project["key"], limit=2, offset=2)]
        self.assertEqual(page1, ["fact 4", "fact 3"])
        self.assertEqual(page2, ["fact 2", "fact 1"])
        self.assertEqual(self.store.active_count(self.project["key"]), 5)

    def test_structured_fields_persist(self):
        from core.ports.distill import DistilledFact

        service.add_records(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s1",
            [
                DistilledFact(
                    text="uses ruff for linting", title="Linting", narrative="Adopted ruff.", files=["pyproject.toml"]
                )
            ],
        )
        row = self.store.rows_for_project(self.project["key"])[0]
        self.assertEqual(row["title"], "Linting")
        self.assertEqual(row["narrative"], "Adopted ruff.")
        self.assertIn("pyproject.toml", row["files"])

    def test_fts_matches_term_present_only_in_title(self):
        from core.ports.distill import DistilledFact

        service.add_records(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s1",
            [DistilledFact(text="the build pipeline", title="Zephyr deploy")],
        )
        self.assertEqual(len(self.store.fts_search(self.project["key"], "zephyr")), 1)

    def test_fts_matches_subtitle_and_files(self):
        from core.ports.distill import Observation, observations_to_facts

        service.add_records(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s1",
            observations_to_facts(
                [
                    Observation(
                        type="feature",
                        title="X",
                        subtitle="uses zephyr indexing",
                        facts=["did a thing"],
                        narrative="",
                        files=["core/widget.py"],
                    )
                ]
            ),
        )
        self.assertEqual(len(self.store.fts_search(self.project["key"], "zephyr")), 1)  # subtitle
        self.assertEqual(len(self.store.fts_search(self.project["key"], "widget")), 1)  # file path

    def test_fts_backfill_on_migration(self):
        from core.ports.distill import DistilledFact

        service.add_records(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s1",
            [DistilledFact(text="the localhost viewer streams updates")],
        )
        # Simulate a database created before the FTS index existed.
        self.store.db.executescript(
            "DROP TABLE facts_fts;DROP TRIGGER facts_ai; DROP TRIGGER facts_ad; DROP TRIGGER facts_au;"
        )
        self.store.db.execute("PRAGMA user_version = 0")
        self.store.db.commit()
        self.store.close()
        reopened = Store(self.cfg.db_path)
        self.assertEqual(len(reopened.fts_search(self.project["key"], "viewer")), 1)
        reopened.close()

    def test_legacy_version_flag_converges_to_head(self):
        from core.ports.distill import DistilledFact
        from core.store import _SCHEMA_VERSION

        service.add_records(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s1",
            [DistilledFact(text="uses github actions for ci")],
        )
        # Simulate a legacy install that only stamped the old FTS flag (=1).
        self.store.db.execute("PRAGMA user_version = 1")
        self.store.db.commit()
        self.store.close()
        reopened = Store(self.cfg.db_path)
        self.assertEqual(reopened.db.execute("PRAGMA user_version").fetchone()[0], _SCHEMA_VERSION)
        self.assertEqual(len(reopened.fts_search(self.project["key"], "github")), 1)
        reopened.close()

    def test_session_summary_replaces_prior(self):
        from core.ports.distill import DistilledFact

        for note in ("Summary one", "Summary two"):
            self.store.clear_session_kind(self.project["key"], "s1", "session_summary")
            service.add_records(
                self.store,
                self.embedder,
                self.cfg,
                self.project,
                "s1",
                [DistilledFact(text=note)],
                kind="session_summary",
            )
        rows = [r for r in self.store.rows_for_project(self.project["key"]) if r["kind"] == "session_summary"]
        self.assertEqual([r["text"] for r in rows], ["Summary two"])

    def test_observation_grouping_persists(self):
        from core.ports.distill import Observation, observations_to_facts

        recs = observations_to_facts(
            [Observation(type="feature", title="Add X", facts=["did a", "did b"], narrative="why")]
        )
        service.add_records(self.store, self.embedder, self.cfg, self.project, "s1", recs)
        rows = self.store.rows_for_project(self.project["key"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["observation_id"] for r in rows}), 1)
        self.assertTrue(all(r["type"] == "feature" for r in rows))

    def test_list_observations_groups_newest_first(self):
        from core.ports.distill import Observation, observations_to_facts

        service.add_records(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s1",
            observations_to_facts([Observation(type="feature", title="A", facts=["a1", "a2"])]),
        )
        service.add_records(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s1",
            observations_to_facts([Observation(type="bugfix", title="B", facts=["b1"])]),
        )
        groups = self.store.list_observations(self.project["key"])
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0][0]["title"], "B")  # newest group first
        self.assertEqual([r["text"] for r in groups[1]], ["a1", "a2"])  # group keeps its facts

    def test_capture_prompts_stores_verbatim(self):
        from core.service import capture_prompts

        prompt = "Please add a subtitle field — 1:1, not distilled."
        n = capture_prompts(self.store, self.embedder, self.cfg, self.project, "s1", [prompt])
        self.assertEqual(n, 1)
        rows = [r for r in self.store.rows_for_project(self.project["key"]) if r["kind"] == "prompt"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], prompt)  # verbatim, unchanged
        self.assertEqual(rows[0]["type"], "prompt")

    def test_dim_divergence_returns_embedding_mismatch(self):
        service.add_facts(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s1",
            ["The deployment pipeline runs on github actions."],
        )
        other_space = HashEmbedding(dim=self.cfg.dim // 2)
        result = service.recall_structured(self.store, other_space, self.cfg, self.project, "deployment pipeline")
        self.assertEqual(result["verdict"], "embedding_mismatch")
        self.assertEqual(result["facts"], [])
        self.assertIn("configuration problem", result["guidance"])

    def test_relevant_recall_is_ok(self):
        service.add_facts(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s1",
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
        result = service.recall_structured(self.store, self.embedder, cfg, self.project, "compact memory storage", k=20)
        used = sum(len(f["text"]) for f in result["facts"])
        self.assertLessEqual(used - len(result["facts"][0]["text"]), 120)
        self.assertEqual(result["dropped"], result["matched"] - result["returned"])


class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        import mcp_server

        self.mcp = mcp_server
        # fresh engine per test so the cached store points at this temp dir
        self.mcp.ENGINE = mcp_server._Engine()

    def tearDown(self):
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def test_initialize_echoes_protocol_and_advertises_tools(self):
        resp = self.mcp._handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        )
        self.assertEqual(resp["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", resp["result"]["capabilities"])

        listed = self.mcp._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in listed["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "recall",
                "list_projects",
                "index_docs",
                "search_docs",
                "get_doc_section",
                "doc_outline",
                "search_code",
                "get_symbol",
                "code_outline",
            },
        )

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


class DistillStructuredTests(unittest.TestCase):
    def test_parse_records_object_wrapped_with_fields(self):
        from core.ports.distill import parse_records

        raw = '{"facts":[{"text":"x","title":"T","narrative":"N","files":["a.py"],"supersedes":[]}]}'
        recs = parse_records(raw)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].title, "T")
        self.assertEqual(recs[0].narrative, "N")
        self.assertEqual(recs[0].files, ["a.py"])

    def test_parse_summary_builds_narrative(self):
        from core.ports.distill import parse_summary

        raw = '{"title":"Did X","request":"do x","learned":"y","completed":"z","next_steps":""}'
        summary = parse_summary(raw)
        self.assertEqual(summary.text, "Did X")
        self.assertIn("Learned: y", summary.narrative)
        self.assertNotIn("Next steps", summary.narrative)

    def test_parse_summary_returns_none_on_junk(self):
        from core.ports.distill import parse_summary

        self.assertIsNone(parse_summary("not json at all"))

    def test_extract_incremental_parts_returns_verbatim_prompt(self):
        import os
        import tempfile

        from core.transcript import extract_incremental_parts

        rows = [
            json.dumps({"type": "user", "message": {"role": "user", "content": "Fix the timezone bug please."}}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Done."},
                            {"type": "tool_use", "name": "Edit", "input": {"file_path": "serve.py"}},
                        ],
                    },
                }
            ),
            json.dumps(
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}}
            ),  # tool result -> not a prompt
        ]
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(rows))
        try:
            text, prompts, _end = extract_incremental_parts(path, 0)
            self.assertEqual(prompts, ["Fix the timezone bug please."])
            self.assertIn("Edited serve.py", text)
        finally:
            os.unlink(path)

    def test_parse_observations_and_expand_to_grouped_facts(self):
        from core.ports.distill import observations_to_facts, parse_observations

        raw = (
            '{"observations":[{"type":"feature","title":"Add X","subtitle":"adds the X capability",'
            '"facts":["did a","did b"],"narrative":"why","files":["a.py"],"supersedes":[]}]}'
        )
        obs = parse_observations(raw)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].type, "feature")
        self.assertEqual(obs[0].subtitle, "adds the X capability")
        self.assertEqual(obs[0].facts, ["did a", "did b"])
        facts = observations_to_facts(obs)
        self.assertEqual([f.text for f in facts], ["did a", "did b"])
        self.assertEqual(len({f.observation_id for f in facts}), 1)  # grouped under one card
        self.assertTrue(all(f.type == "feature" and f.narrative == "why" for f in facts))
        self.assertTrue(all(f.subtitle == "adds the X capability" for f in facts))
        self.assertEqual([f.supersedes for f in facts], [[], []])  # obs had none

    def test_parse_observations_defaults_unknown_type(self):
        from core.ports.distill import parse_observations

        obs = parse_observations('{"observations":[{"type":"nonsense","facts":["x"]}]}')
        self.assertEqual(obs[0].type, "discovery")


if __name__ == "__main__":
    unittest.main(verbosity=2)
