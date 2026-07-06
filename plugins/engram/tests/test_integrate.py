"""Integrate stage tests — REM-style near-duplicate merge.

Pure clustering (`cluster_duplicates`), the heuristic-floor shell `integrate` (default-off,
archives STM near-duplicates into a survivor, reversible `status='merged'`, STM-only), and
the opt-in LLM tier (abstract / veto / fail-open) via a stub distiller. Stdlib unittest;
vectors are crafted so the cosine cut is deterministic.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.consolidation.integrate import cluster_duplicates, integrate  # noqa: E402
from core.domain.quantize import pack_bits, quantize_int8  # noqa: E402
from core.ports.distill import Distiller  # noqa: E402
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.store import Store  # noqa: E402


class _FakeMerger(Distiller):
    """Distiller stub whose merge_cluster returns a fixed value (or raises) — LLM-tier test."""

    def __init__(self, result):
        self._result = result
        self.calls: list[list[str]] = []

    def distill(self, text, existing):
        return []

    def merge_cluster(self, texts):
        self.calls.append(list(texts))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class ClusterDuplicatesTests(unittest.TestCase):
    """Pure clustering — deterministic over hand-crafted vectors, no I/O."""

    def test_groups_near_identical_and_leaves_singletons(self):
        items = [("a", [1.0, 0.0]), ("b", [0.99, 0.01]), ("c", [0.0, 1.0])]
        self.assertEqual(cluster_duplicates(items, 0.9), [("a", ["b"])])

    def test_survivor_is_first_in_order(self):
        # b comes first -> b survives, a is absorbed.
        items = [("b", [0.99, 0.01]), ("a", [1.0, 0.0])]
        self.assertEqual(cluster_duplicates(items, 0.9), [("b", ["a"])])

    def test_no_clusters_when_below_threshold(self):
        items = [("a", [1.0, 0.0]), ("b", [0.0, 1.0])]
        self.assertEqual(cluster_duplicates(items, 0.9), [])

    def test_each_absorbed_only_once(self):
        items = [("a", [1.0, 0.0]), ("b", [1.0, 0.0]), ("c", [1.0, 0.0])]
        # a survives, b and c both absorbed into a's cluster (not re-formed under b).
        self.assertEqual(cluster_duplicates(items, 0.9), [("a", ["b", "c"])])


class IntegrateShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        # Pin integrate_threshold to the code default (off) so the suite is hermetic against an
        # ambient ENGRAM_INTEGRATE_THRESHOLD in the environment; the "on" tests override it locally.
        self.cfg = replace(get_config(), distiller="heuristic", integrate_threshold=0)
        self.store = Store(self.cfg.db_path)
        self.project = {"key": "test", "path": "/tmp/test", "label": "test"}

    def tearDown(self):
        self.store.close()
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def _add_vec(self, text, vec, tier="stm"):
        blob, scale = quantize_int8(vec)
        self.store.add(
            project=self.project,
            session_id="s1",
            kind="fact",
            text=text,
            vec_int8=blob,
            scale=scale,
            dim=len(vec),
            vec_bits=pack_bits(vec),
            importance=0.5,
            tier=tier,
        )
        return self.store.fact_id(self.project["key"], text)

    def test_disabled_by_default(self):
        self._add_vec("dup one", [1.0, 0.0, 0.0, 0.0])
        self._add_vec("dup two", [0.98, 0.02, 0.0, 0.0])
        self.assertEqual(integrate(self.store, self.cfg, self.project), 0)  # threshold 0 = off
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 2)

    def test_merges_near_duplicates_reversibly(self):
        a = self._add_vec("dup one", [1.0, 0.0, 0.0, 0.0])
        b = self._add_vec("dup two", [0.98, 0.02, 0.0, 0.0])
        distinct = self._add_vec("unrelated", [0.0, 0.0, 1.0, 0.0])
        cfg = replace(self.cfg, integrate_threshold=0.9)
        merged = integrate(self.store, cfg, self.project)
        self.assertEqual(merged, 1)  # one of {a,b} absorbed
        statuses = {r["status"] for r in (self.store.get(a), self.store.get(b))}
        self.assertEqual(statuses, {"active", "merged"})  # exactly one survivor
        self.assertEqual(self.store.get(distinct)["status"], "active")  # distinct untouched
        active = {r["id"] for r in self.store.active_rows_for_project(self.project["key"])}
        self.assertEqual(len(active), 2)  # survivor + distinct; merged leaves the search set

    def test_survivor_is_the_reinforced_fact(self):
        a = self._add_vec("weak dup", [1.0, 0.0, 0.0, 0.0])
        b = self._add_vec("strong dup", [0.98, 0.02, 0.0, 0.0])
        self.store.reinforce(b)  # frequency 1 -> 2, so b outranks a as survivor
        cfg = replace(self.cfg, integrate_threshold=0.9)
        integrate(self.store, cfg, self.project)
        self.assertEqual(self.store.get(b)["status"], "active")  # reinforced fact survives
        self.assertEqual(self.store.get(a)["status"], "merged")

    def test_ltm_facts_are_not_merge_candidates(self):
        self._add_vec("dup one", [1.0, 0.0, 0.0, 0.0], tier="ltm")
        self._add_vec("dup two", [0.98, 0.02, 0.0, 0.0], tier="ltm")
        cfg = replace(self.cfg, integrate_threshold=0.9)
        self.assertEqual(integrate(self.store, cfg, self.project), 0)  # STM-only pool

    # --- LLM tier (opt-in): abstract / veto / fail-open ---

    def _llm_cfg(self):
        return replace(self.cfg, distiller="ollama", integrate_threshold=0.9)

    def test_llm_tier_abstracts_cluster_into_one_fact(self):
        a = self._add_vec("dup one", [1.0, 0.0, 0.0, 0.0])
        b = self._add_vec("dup two", [0.98, 0.02, 0.0, 0.0])
        fake = _FakeMerger("unified merged fact")
        merged = integrate(
            self.store, self._llm_cfg(), self.project, embedder=HashEmbedding(dim=self.cfg.dim), distiller=fake
        )
        self.assertEqual(merged, 2)  # both originals archived
        self.assertEqual(self.store.get(a)["status"], "merged")
        self.assertEqual(self.store.get(b)["status"], "merged")
        active = {r["text"] for r in self.store.active_rows_for_project(self.project["key"])}
        self.assertEqual(active, {"unified merged fact"})  # the fresh abstraction is the survivor
        self.assertTrue(fake.calls)  # the distiller was consulted

    def test_llm_tier_veto_keeps_cluster_separate(self):
        self._add_vec("dup one", [1.0, 0.0, 0.0, 0.0])
        self._add_vec("dup two", [0.98, 0.02, 0.0, 0.0])
        fake = _FakeMerger(None)  # LLM says "DISTINCT"
        merged = integrate(
            self.store, self._llm_cfg(), self.project, embedder=HashEmbedding(dim=self.cfg.dim), distiller=fake
        )
        self.assertEqual(merged, 0)
        active = {r["text"] for r in self.store.active_rows_for_project(self.project["key"])}
        self.assertEqual(active, {"dup one", "dup two"})  # nothing merged

    def test_llm_tier_error_falls_back_to_heuristic_floor(self):
        a = self._add_vec("dup one", [1.0, 0.0, 0.0, 0.0])
        b = self._add_vec("dup two", [0.98, 0.02, 0.0, 0.0])
        fake = _FakeMerger(RuntimeError("llm unreachable"))
        merged = integrate(
            self.store, self._llm_cfg(), self.project, embedder=HashEmbedding(dim=self.cfg.dim), distiller=fake
        )
        self.assertEqual(merged, 1)  # blunt floor: absorbed archived, survivor kept
        statuses = sorted([self.store.get(a)["status"], self.store.get(b)["status"]])
        self.assertEqual(statuses, ["active", "merged"])
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 1)  # no new abstraction added


if __name__ == "__main__":
    unittest.main()
