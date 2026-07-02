"""End-to-end smoke tests — stdlib unittest, no external deps.

Run: python3 -m unittest discover -s plugins/ltm/tests  (from repo root)
 or: python3 plugins/ltm/tests/test_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.distill import (  # noqa: E402
    ClaudeCliDistiller,
    DistilledFact,
    HTTPDistiller,
    HeuristicDistiller,
    get_distiller,
    parse_records,
)
from core.embedding import HashEmbedding  # noqa: E402
from core.project import resolve_project  # noqa: E402
from core.quantize import cosine, dequantize_int8, hamming, pack_bits, quantize_int8  # noqa: E402
from core.recall import search  # noqa: E402
from core.scoring import frequency_boost, priority, recency_decay  # noqa: E402
from core.store import Store  # noqa: E402
from core import service  # noqa: E402


class QuantizeTests(unittest.TestCase):
    def test_int8_roundtrip_preserves_direction(self):
        emb = HashEmbedding(dim=128)
        vec = emb.embed_one("the quick brown fox jumps over the lazy dog")
        blob, scale = quantize_int8(vec)
        self.assertGreater(cosine(vec, dequantize_int8(blob, scale)), 0.98)

    def test_binary_pack_length_and_hamming(self):
        vec = [0.5, -0.5, 0.1, -0.9, 0.0, 0.2, -0.3, 0.7, 0.4]
        bits = pack_bits(vec)
        self.assertEqual(len(bits), (len(vec) + 7) // 8)
        self.assertEqual(hamming(bits, bits), 0)


class ScoringTests(unittest.TestCase):
    def test_recency_decay_curve(self):
        self.assertAlmostEqual(recency_decay(0, 30), 1.0, places=6)
        self.assertAlmostEqual(recency_decay(30 * 86400, 30), 0.5, places=3)
        self.assertLess(recency_decay(90 * 86400, 30), recency_decay(1 * 86400, 30))

    def test_frequency_boost_monotonic(self):
        self.assertEqual(frequency_boost(1), 0.0)
        self.assertGreater(frequency_boost(4), frequency_boost(2))

    def test_priority_weights(self):
        self.assertGreater(priority(0.9, 0.5, 0.5, 1, 0.3, 0.2), priority(0.1, 0.5, 0.5, 1, 0.3, 0.2))


class DistillerTests(unittest.TestCase):
    def test_parse_records_handles_codefence(self):
        raw = 'sure!\n```json\n[{"text": "uses Rust", "supersedes": ["abc"]}]\n```\n'
        records = parse_records(raw)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].text, "uses Rust")
        self.assertEqual(records[0].supersedes, ["abc"])

    def test_parse_records_bad_input_returns_empty(self):
        self.assertEqual(parse_records("no json here"), [])

    def test_parse_records_filters_sentinel_supersedes(self):
        raw = '[{"text": "uses DynamoDB", "supersedes": "none"}, {"text": "x", "supersedes": ["", "abc", "null"]}]'
        records = parse_records(raw)
        self.assertEqual(records[0].supersedes, [])
        self.assertEqual(records[1].supersedes, ["abc"])

    def test_get_distiller_selects_backend(self):
        cfg = get_config()
        self.assertIsInstance(get_distiller(replace(cfg, distiller="claude")), ClaudeCliDistiller)
        self.assertIsInstance(get_distiller(replace(cfg, distiller="heuristic")), HeuristicDistiller)
        self.assertIsInstance(get_distiller(replace(cfg, distiller="ollama")), HTTPDistiller)

    def test_http_distiller_falls_back_when_unreachable(self):
        distiller = HTTPDistiller("http://127.0.0.1:1/v1", "any-model", timeout=1)
        records = distiller.distill("we decided to adopt the repository pattern for data access", [])
        self.assertTrue(records)
        self.assertTrue(all(isinstance(r, DistilledFact) for r in records))


class ProvisionTests(unittest.TestCase):
    def test_find_base_python_returns_valid_or_none(self):
        from core.provision import find_base_python

        exe = find_base_python()
        self.assertTrue(exe is None or os.path.exists(exe))

    def test_not_provisioned_on_empty_dir(self):
        from core.provision import is_provisioned, venv_python

        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(is_provisioned(tmp))
            self.assertIn("python", str(venv_python(tmp)).lower())

    def test_requirements_declared(self):
        from core.provision import requirements

        reqs = requirements()
        self.assertTrue(reqs)
        self.assertTrue(any("fastembed" in r for r in reqs))

    def test_reexec_is_noop_without_pin(self):
        sys.path.insert(0, str(ROOT / "bin"))
        import _bootstrap

        os.environ.pop("CLAUDE_PLUGIN_OPTION_python", None)
        os.environ.pop("LTM_PYTHON", None)
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["LTM_DATA_DIR"] = tmp  # no managed venv here
            try:
                _bootstrap.reexec_if_pinned()  # must return without exec/raise
            finally:
                os.environ.pop("LTM_DATA_DIR", None)


class ProjectTests(unittest.TestCase):
    def test_marker_walk_finds_root_from_subdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "myrepo"
            (root / ".git").mkdir(parents=True)
            deep = root / "apps" / "web" / "src"
            deep.mkdir(parents=True)
            self.assertEqual(
                resolve_project(str(deep), (".git",))["key"],
                resolve_project(str(root), (".git",))["key"],
            )
            self.assertEqual(resolve_project(str(deep), (".git",))["label"], "myrepo")


class LoopTests(unittest.TestCase):
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

    def test_capture_recall_and_reinforcement(self):
        text = "\n".join(
            [
                "Recall injects memory via additionalContext just-in-time.",
                "Capture runs in a detached worker with zero interactive tokens.",
                "Embeddings are quantised to int8 to keep the store compact.",
            ]
        )
        self.assertEqual(service.capture_text(self.store, self.embedder, self.cfg, self.project, "s1", text), 3)
        # Re-capture adds no rows but reinforces (consolidation).
        self.assertEqual(service.capture_text(self.store, self.embedder, self.cfg, self.project, "s1", text), 0)
        fid = self.store.fact_id(self.project["key"], "Embeddings are quantised to int8 to keep the store compact.")
        self.assertEqual(self.store.get(fid)["frequency"], 2)

        block = service.recall_prompt_block(
            self.store, self.embedder, self.cfg, self.project, "how is memory injected into context"
        )
        self.assertIn("additionalContext", block)

    def test_supersession_newest_wins(self):
        cfg = replace(self.cfg, supersede_threshold=0.5)
        service.capture_text(self.store, self.embedder, cfg, self.project, "s1", "The project language is Python.")
        service.capture_text(self.store, self.embedder, cfg, self.project, "s2", "The project language is Rust.")
        active = [r["text"] for r in self.store.active_rows_for_project(self.project["key"])]
        self.assertIn("The project language is Rust.", active)
        self.assertNotIn("The project language is Python.", active)
        block = service.recall_prompt_block(self.store, self.embedder, cfg, self.project, "what language is the project")
        self.assertIn("Rust", block)
        self.assertNotIn("Python", block)

    def test_recency_breaks_ties(self):
        # Two equally-similar facts; the newer one must rank first.
        old = time.time() - 100 * 86400
        for text, stamp in (("alpha config uses value one", old), ("alpha config uses value two", time.time())):
            vec = self.embedder.embed_one(text)
            blob, scale = quantize_int8(vec)
            self.store.add(
                project=self.project, session_id="s", kind="fact", text=text,
                vec_int8=blob, scale=scale, dim=len(vec), vec_bits=pack_bits(vec),
                importance=0.5, created_at=stamp,
            )
        hits = search(self.store, self.embedder, self.project, "alpha config value", self.cfg, k=2, min_sim=-1.0)
        self.assertEqual(hits[0][1]["text"], "alpha config uses value two")

    def test_explicit_supersedes_handles_disjoint_vocabulary(self):
        # The Paris/London case: no shared words, so similarity can't catch it —
        # only an explicit supersedes link (from the LLM distiller) can.
        cfg = replace(self.cfg, supersede_threshold=1.0)  # similarity supersession off
        service.add_facts(self.store, self.embedder, cfg, self.project, "s1", ["I live in Paris."])
        paris_id = self.store.fact_id(self.project["key"], "I live in Paris.")
        record = DistilledFact("I moved to London.", supersedes=[paris_id])
        service.add_records(self.store, self.embedder, cfg, self.project, "s2", [record])
        active = [r["text"] for r in self.store.active_rows_for_project(self.project["key"])]
        self.assertIn("I moved to London.", active)
        self.assertNotIn("I live in Paris.", active)

    def test_ttl_sweep_expires_stale_low_frequency_facts(self):
        now = time.time()
        specs = [
            ("stale detail about a temporary experiment", now - 40 * 86400, 1),
            ("fresh note about the current task", now, 1),
            ("durable convention seen across many sessions", now - 40 * 86400, 5),
        ]
        for text, when, freq in specs:
            vec = self.embedder.embed_one(text)
            blob, scale = quantize_int8(vec)
            self.store.add(
                project=self.project, session_id="s", kind="fact", text=text,
                vec_int8=blob, scale=scale, dim=len(vec), vec_bits=pack_bits(vec),
                importance=0.5, created_at=when,
            )
            fid = self.store.fact_id(self.project["key"], text)
            self.store.db.execute("UPDATE facts SET frequency = ?, last_seen = ? WHERE id = ?", (freq, when, fid))
        self.store.db.commit()

        expired = self.store.sweep(now, 30 * 86400, keep_frequency=3, project_key=self.project["key"])
        active = [r["text"] for r in self.store.active_rows_for_project(self.project["key"])]
        self.assertEqual(expired, 1)
        self.assertNotIn("stale detail about a temporary experiment", active)
        self.assertIn("fresh note about the current task", active)
        self.assertIn("durable convention seen across many sessions", active)

    def test_search_skips_mismatched_embedding_dims(self):
        # A row from a different embedder (wrong dim) must be ignored, not crash.
        good = "the deployment pipeline runs on github actions"
        service.add_facts(self.store, self.embedder, self.cfg, self.project, "s1", [good])
        stray = [0.1] * 8
        blob, scale = quantize_int8(stray)
        self.store.add(
            project=self.project, session_id="s2", kind="fact", text="stray wrong-dim row",
            vec_int8=blob, scale=scale, dim=8, vec_bits=pack_bits(stray), importance=0.5,
        )
        hits = search(self.store, self.embedder, self.project, "deployment pipeline", self.cfg, k=10, min_sim=-1.0)
        texts = [r["text"] for _s, r in hits]
        self.assertIn(good, texts)
        self.assertNotIn("stray wrong-dim row", texts)

    def test_recall_respects_char_budget(self):
        facts = [f"fact number {i} about compact memory storage systems" for i in range(20)]
        service.add_facts(self.store, self.embedder, self.cfg, self.project, "s2", facts)
        cfg = replace(self.cfg, max_chars=120, top_k=20)
        block = service.recall_prompt_block(self.store, self.embedder, cfg, self.project, "compact memory storage")
        self.assertLessEqual(len(block), 120)


if __name__ == "__main__":
    unittest.main(verbosity=2)
