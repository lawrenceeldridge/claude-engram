"""Usage ledger — the two-sided token accounting behind `ltm stats`.

Cost side: injections recorded by recall_prompt_block / recall_core_block. Saving side
aggregation via usage_stats. Stdlib unittest, hash embedder, no network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import service  # noqa: E402
from core.config import get_config  # noqa: E402
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.store import Store  # noqa: E402


class UsageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LTM_DATA_DIR"] = self.tmp.name
        self.cfg = replace(get_config(), distiller="heuristic", min_sim=-1.0)  # always inject a match
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}

    def tearDown(self):
        self.store.close()
        os.environ.pop("LTM_DATA_DIR", None)
        self.tmp.cleanup()

    def _add(self, text: str) -> None:
        service.add_facts(self.store, self.embedder, self.cfg, self.project, "s", [text])

    def test_record_and_aggregate(self):
        self.store.record_usage("p", "inject_prompt", bytes_in=120)
        self.store.record_usage("p", "inject_prompt", bytes_in=80)
        self.store.record_usage("p", "pull_symbol", bytes_saved=500)
        s = self.store.usage_stats("p")
        self.assertEqual(s["inject_prompt"], {"n": 2, "bytes_in": 200, "bytes_saved": 0})
        self.assertEqual(s["pull_symbol"]["bytes_saved"], 500)

    def test_recall_prompt_block_records_injection_cost(self):
        self._add("the deploy target is fly.io")
        block = service.recall_prompt_block(self.store, self.embedder, self.cfg, self.project, "deploy target")
        self.assertTrue(block)
        entry = self.store.usage_stats("p")["inject_prompt"]
        self.assertEqual(entry["n"], 1)
        self.assertEqual(entry["bytes_in"], len(block))

    def test_recall_core_block_records_injection_cost(self):
        self._add("fact one")
        block = service.recall_core_block(self.store, self.cfg, self.project)
        self.assertTrue(block)
        self.assertEqual(self.store.usage_stats("p")["inject_core"]["n"], 1)

    def test_empty_block_records_nothing(self):
        block = service.recall_prompt_block(self.store, self.embedder, self.cfg, self.project, "nothing stored")
        self.assertEqual(block, "")  # empty store → Null Object → no cost row
        self.assertEqual(self.store.usage_stats("p"), {})

    def test_usage_summary_computes_net(self):
        self.store.record_usage("p", "inject_prompt", bytes_in=400)  # 100 tokens cost
        self.store.record_usage("p", "pull_symbol", bytes_saved=4000)  # 1000 tokens measured saving
        self.store.log_recall("p", "q", returned=1, top_sim=0.9, confidence=0.9, verdict="ok")  # 1200 est
        s = service.usage_summary(self.store, "p")
        self.assertEqual(s["cost_tokens"], 100)
        self.assertEqual(s["saved_measured_tokens"], 1000)
        self.assertEqual(s["saved_estimated_tokens"], 1200)
        self.assertEqual(s["net_tokens"], 1000 + 1200 - 100)
        self.assertEqual(s["injections"], 1)
        self.assertEqual(s["targeted_reads"], 1)

    def test_usage_summary_cache_adjusted_cost_and_measured_net(self):
        # Prompt injections stay at face value; the core block pays the cache-write
        # premium plus a discounted re-read per recorded turn (recall events).
        self.store.record_usage("p", "inject_prompt", bytes_in=400)  # 100 tokens
        self.store.record_usage("p", "inject_core", bytes_in=4000)  # 1000 tokens, one session
        for _ in range(10):  # 10 turns in that session
            self.store.log_recall("p", "q", returned=0, top_sim=0.0, confidence=0.0, verdict="no_memory")
        self.store.record_usage("p", "pull_symbol", bytes_saved=40000)  # 10000 tokens measured
        s = service.usage_summary(self.store, "p")
        # 100 + 1000*1.25 + (1000/1)*0.1*10 = 100 + 1250 + 1000 = 2350
        self.assertEqual(s["cost_tokens"], 1100)
        self.assertEqual(s["cost_tokens_cache_adjusted"], 2350)
        # Headline excludes the estimate and charges the larger cost view.
        self.assertEqual(s["net_measured_tokens"], 10000 - 2350)

    def test_usage_summary_headline_excludes_estimated_savings(self):
        self.store.record_usage("p", "pull_symbol", bytes_saved=4000)  # 1000 tokens measured
        self.store.log_recall("p", "q", returned=1, top_sim=0.9, confidence=0.9, verdict="ok")  # est only
        s = service.usage_summary(self.store, "p")
        self.assertEqual(s["net_measured_tokens"], 1000)  # 1200-token estimate not in headline
        self.assertEqual(s["net_tokens"], 1000 + 1200)  # blended figure keeps it

    def test_usage_summary_empty_ledger_is_all_zero(self):
        s = service.usage_summary(self.store, "p")
        self.assertEqual(s["cost_tokens"], 0)
        self.assertEqual(s["cost_tokens_cache_adjusted"], 0)
        self.assertEqual(s["net_measured_tokens"], 0)

    def test_usage_summary_counts_bounded_reads_as_measured(self):
        self.store.record_usage("p", "pull_symbol", bytes_saved=4000)  # 1000 tokens
        self.store.record_usage("p", "read_bounded", bytes_saved=8000)  # 2000 tokens (bounded Read)
        s = service.usage_summary(self.store, "p")
        self.assertEqual(s["saved_measured_tokens"], 3000)  # both count as measured
        self.assertEqual(s["targeted_reads"], 1)  # ltm-tool pulls only
        self.assertEqual(s["bounded_reads"], 1)  # surfaced separately


class CreditReadHookTests(unittest.TestCase):
    """bin/credit_read.py — books a bounded Read of an indexed file as a measured saving."""

    def setUp(self):
        self.data = tempfile.TemporaryDirectory()
        self.proj = tempfile.TemporaryDirectory()
        (Path(self.proj.name) / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        self.file = Path(self.proj.name) / "mod.py"
        self.file.write_text("def foo():\n    return 1\n" * 60, encoding="utf-8")  # a sizable indexed file
        os.environ["LTM_DATA_DIR"] = self.data.name
        self.env = {
            **os.environ,
            "LTM_DATA_DIR": self.data.name,
            "LTM_EMBEDDING": "hash",
            "LTM_ENFORCE": "off",
            "LTM_BUS": "inproc",
        }
        self.env.pop("LTM_PYTHON", None)

    def tearDown(self):
        os.environ.pop("LTM_DATA_DIR", None)
        self.data.cleanup()
        self.proj.cleanup()

    def _index(self) -> dict:
        from core.index.indexer import index_file
        from core.ports.embedding import get_embedder
        from core.project import resolve_project

        cfg = get_config()
        store = Store(cfg.db_path)
        project = resolve_project(str(self.file.parent), cfg.markers)
        index_file(store, get_embedder(cfg), cfg, project, str(self.file))
        store.close()
        return project

    def _run_hook(self, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "bin" / "credit_read.py")],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=30,
            env=self.env,
        )

    def _bounded(self, project_key: str) -> dict:
        store = Store(get_config().db_path)
        try:
            return store.usage_stats(project_key).get("read_bounded", {})
        finally:
            store.close()

    def _payload(self, **tool_input) -> dict:
        return {
            "tool_name": "Read",
            "tool_input": {"file_path": str(self.file), **tool_input},
            "tool_response": "def foo():\n    return 1\n",  # the small returned span
        }

    def test_credits_bounded_read_of_indexed_file(self):
        project = self._index()
        proc = self._run_hook(self._payload(offset=1, limit=5))
        self.assertEqual(proc.returncode, 0)
        entry = self._bounded(project["key"])
        self.assertEqual(entry.get("n"), 1)
        self.assertGreater(entry.get("bytes_saved", 0), 0)  # file - returned span

    def test_no_credit_for_whole_file_read(self):
        project = self._index()
        self._run_hook(self._payload())  # no offset/limit
        self.assertEqual(self._bounded(project["key"]), {})

    def test_no_credit_for_unindexed_file(self):
        project = {"key": "x"}  # not indexed; hook resolves its own key, finds no source_state
        self._run_hook(self._payload(offset=1, limit=5))
        # nothing indexed at all → no read_bounded row for any project
        store = Store(get_config().db_path)
        try:
            self.assertEqual(store.usage_stats(project["key"]).get("read_bounded", {}), {})
        finally:
            store.close()

    def test_fail_open_on_bad_payload(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "credit_read.py")],
            input="not json",
            text=True,
            capture_output=True,
            timeout=30,
            env=self.env,
        )
        self.assertEqual(proc.returncode, 0)  # never raises


if __name__ == "__main__":
    unittest.main()
