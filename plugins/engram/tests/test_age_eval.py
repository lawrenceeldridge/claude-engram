"""Age-aware ranking benchmark tests — stdlib, hash embedder, temp stores.

The benchmark decides whether recency weights change, so its plumbing is pinned: ages are
seeded and in range, the old/new split excludes mixed-gold queries, both rankers are the
production paths, and the fusion-weight override is scoped (never leaks into later code).
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from _harness import temp_data_dir

from bench.age_eval import (
    DAY,
    NEW_DAYS,
    OLD_DAYS,
    evaluate_aged,
    fused_variants,
    hook_variants,
    split_by_age,
    stamp_ages,
)
from bench.distractors import load_distractors
from bench.retrieval import fused_ranker, search_ranker
from bench.stores import build_store
from core.config import get_config
from core.domain import fusion
from core.ports.embedding import HashEmbedding
from core.recall import search, search_fused


class StampTests(unittest.TestCase):
    def test_seeded_balanced_and_in_range(self):
        facts = [f"fact {i}" for i in range(400)]
        records, old = stamp_ages(facts, now=1_000_000_000.0, seed=7)
        self.assertEqual((records, old), stamp_ages(facts, now=1_000_000_000.0, seed=7))
        self.assertTrue(150 < len(old) < 250)  # a fair coin over 400 facts
        for index, (_text, ts) in enumerate(records):
            age_days = (1_000_000_000.0 - ts) / DAY
            lo, hi = OLD_DAYS if index in old else NEW_DAYS
            self.assertTrue(lo <= age_days <= hi)

    def test_split_excludes_mixed_age_gold(self):
        queries = [
            {"q": "a", "relevant": [0]},
            {"q": "b", "relevant": [1]},
            {"q": "c", "relevant": [0, 1]},  # mixed: one old, one new
            {"q": "d", "relevant": []},
        ]
        old_q, new_q = split_by_age(queries, old={0})
        self.assertEqual(([q["q"] for q in old_q], [q["q"] for q in new_q]), (["a"], ["b"]))


class RankerAndEvalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_data_dir(self)
        self.cfg = get_config()
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.facts = [
            "The deployment pipeline runs on github actions with a manual approval gate.",
            "Frontend styling uses tailwind utility classes.",
            "The sqlite store keeps int8 vectors per fact.",
            "Recall injects at most three facts per prompt.",
        ]

    def test_rankers_are_the_production_paths(self):
        store, project = build_store(self.embedder, self.cfg, [(t, None) for t in self.facts], Path(self.tmp.name))
        try:
            query = "deployment approval"
            direct = [
                row["text"] for _s, row in search(store, self.embedder, project, query, self.cfg, k=10, min_sim=-1.0)
            ]
            fused = [
                row["text"]
                for _s, _sim, row in search_fused(store, self.embedder, project, query, self.cfg, k=10, min_sim=-1.0)
            ]
            self.assertEqual(search_ranker(store, self.embedder, project, self.cfg)(query), direct)
            self.assertEqual(fused_ranker(store, self.embedder, project, self.cfg)(query), fused)
        finally:
            store.close()

    def test_evaluate_reports_every_variant_and_restores_fusion_weights(self):
        before = dict(fusion.DEFAULT_WEIGHTS)
        data = {
            "facts": self.facts,
            "queries": [
                {"q": "deployment pipeline approval", "relevant": [0]},
                {"q": "tailwind styling", "relevant": [1]},
            ],
        }
        result = evaluate_aged("hash", data, self.cfg, distractors=[])
        self.assertEqual(fusion.DEFAULT_WEIGHTS, before)  # the scoped override never leaks
        hook, fused = hook_variants(self.cfg), fused_variants()
        self.assertEqual(len(result["rows"]), len(hook) + len(fused))
        self.assertEqual(result["old_n"] + result["new_n"], 2)
        shipped = [r for r in result["rows"] if r["new dR@3 vs shipped"] == "—"]
        blind = [r for r in result["rows"] if r["old dR@3 vs age-blind"] == "—"]
        self.assertEqual([r["variant"] for r in shipped], [hook[0][0], fused[0][0]])
        self.assertEqual([r["variant"] for r in blind], [hook[-1][0], fused[-1][0]])

    def test_age_blind_variants_switch_recency_off(self):
        hook, fused = hook_variants(self.cfg), fused_variants()
        self.assertEqual(hook[-1][1], {"w_recency": 0.0})
        self.assertEqual(fused[-1][1], {"recency": 0.0})
        self.assertEqual((hook[0][1], fused[0][1]), ({}, {}))  # shipped = no overrides

    def test_shipped_label_follows_the_live_defaults_and_no_row_repeats_it(self):
        hook = hook_variants(replace(self.cfg, w_recency=0.05, half_life_days=30.0))
        self.assertEqual(hook[0][0], "shipped (w_recency 0.05, half-life 30d)")
        self.assertNotIn({"w_recency": 0.05}, [o for _label, o in hook])  # equal to shipped → dropped
        self.assertIn(("w_recency 0.3", {"w_recency": 0.3}), hook)  # the previous default stays comparable
        with mock.patch.dict(fusion.DEFAULT_WEIGHTS, {"recency": 0.2}):
            fused = fused_variants()
        self.assertEqual([label for label, _o in fused], ["shipped (recency 0.2)", "age-blind (recency 0)"])

    def test_plus_float_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_aged("hash+float", {"facts": [], "queries": []}, self.cfg, distractors=[])


class LoadDistractorsTests(unittest.TestCase):
    def test_zero_requested_mines_nothing(self):
        args = argparse.Namespace(distractors=0, store_project=None, store_db=None)
        self.assertEqual(load_distractors(args, get_config(), exclude=[]), [])

    def test_padding_without_a_source_project_is_refused(self):
        args = argparse.Namespace(distractors=10, store_project=None, store_db=None)
        self.assertIsNone(load_distractors(args, get_config(), exclude=[]))

    def test_a_missing_source_db_is_refused_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(distractors=10, store_project="p", store_db=str(Path(tmp) / "none.db"))
            self.assertIsNone(load_distractors(args, get_config(), exclude=[]))


if __name__ == "__main__":
    unittest.main()
