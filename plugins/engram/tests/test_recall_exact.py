"""The recall hot path's exact shortcuts (#66 part 2): each must equal its plain definition.

``overlap_counts`` vs per-text ``token_set``; ``top_by_priority`` vs scoring and sorting every
row; ``fuse(limit=k)`` vs the full fusion; the lean ``Store.scan_rows`` vs the full-row query;
and the shipped ``search`` / ``search_fused_with_stats`` returning full (hydrated) rows.
"""

from __future__ import annotations

import random
import unittest
from dataclasses import replace

from _harness import temp_data_dir

from core import service
from core.config import get_config
from core.domain.fusion import DEFAULT_SMOOTHING, DEFAULT_WEIGHTS, Channel, fuse
from core.domain.lexical import overlap_counts, token_set
from core.domain.scoring import fact_priority, top_by_priority
from core.ports.distill import DistilledFact
from core.ports.embedding import HashEmbedding
from core.ports.scorer import DIM_MISMATCH, get_scorer
from core.project import GLOBAL_PROJECT_KEY
from core.recall import _hydrate, _recall_rows, _score, search, search_fused_with_stats
from core.store import Store

WORDS = ["deploy", "lambda", "pipeline", "int8", "recall", "store", "the", "and", "aws", "düsseldorf", "café"]


def _definition_overlap(query_tokens: set[str], texts: list[str]) -> list[int]:
    return [len(query_tokens & token_set(text)) for text in texts]


class OverlapCountsTests(unittest.TestCase):
    def test_equals_the_per_text_definition_on_random_texts(self):
        rng = random.Random(7)
        for _ in range(200):
            texts = [
                " ".join(rng.choices(WORDS + ["x", "deployment", "re-call", "AWS!"], k=rng.randint(0, 12)))
                for _ in range(rng.randint(0, 30))
            ]
            query = token_set(" ".join(rng.choices(WORDS, k=rng.randint(0, 4))))
            self.assertEqual(overlap_counts(query, texts), _definition_overlap(query, texts))

    def test_boundaries_and_repeats(self):
        query = {"deploy", "aws"}
        texts = ["deployment", "redeploy", "deploy-ok", "deploy deploy", "aws_lambda", "AWS", "", "deploy\nAWS"]
        self.assertEqual(overlap_counts(query, texts), _definition_overlap(query, texts))
        self.assertEqual(overlap_counts(query, texts), [0, 0, 1, 1, 1, 1, 0, 2])

    def test_unicode_including_a_length_changing_lower_case(self):
        query = {"sseldorf", "stanbul", "cafe"}
        texts = ["Düsseldorf office", "İstanbul trip", "café", "STRASSE straße", "istanbul"]
        self.assertNotEqual(len("İ".lower()), 1)  # the case that forces the per-text path
        self.assertEqual(overlap_counts(query, texts), _definition_overlap(query, texts))

    def test_a_text_holding_the_separator_and_non_content_query_tokens(self):
        texts = ["deploy\x00aws", "the aws", "c++ deploy"]
        for query in ({"deploy", "aws"}, {"the", "c++", "ab", "aws"}, set()):
            self.assertEqual(overlap_counts(query, texts), _definition_overlap(query, texts))
        self.assertEqual(overlap_counts({"aws"}, []), [])


def _definition_top(rows, sims, k, min_sim, now, half_life, weights):
    scored = [(fact_priority(r, s, now, half_life, weights), r) for r, s in zip(rows, sims) if s >= min_sim]
    scored.sort(key=lambda hit: hit[0], reverse=True)
    return scored[:k]


class TopByPriorityTests(unittest.TestCase):
    def _rows(self, rng, n):
        return [
            {
                "id": f"f{i}",
                "last_seen": rng.choice([None, rng.uniform(0, 1000)]),
                "created_at": rng.uniform(0, 1000),
                "frequency": rng.choice([None, 1, 2, 5, 9]),
            }
            for i in range(n)
        ]

    def test_equals_scoring_and_sorting_every_row(self):
        rng = random.Random(11)
        for trial in range(300):
            n = rng.randint(0, 60)
            rows = self._rows(rng, n)
            sims = [rng.choice([DIM_MISMATCH, 0.5, 0.5, rng.uniform(-0.2, 1.0)]) for _ in range(n)]  # ties + mismatches
            weights = rng.choice([(1.0, 0.05, 0.1), (1.0, 0.0, 0.0), (0.0, 1.0, 1.0), (0.7, 0.3, 0.2)])
            k, min_sim = rng.randint(0, 6), rng.choice([0.12, -1.0, 0.6])
            args = dict(min_sim=min_sim, now=1200.0, half_life_days=rng.choice([0.0, 0.01, 30.0]), weights=weights)
            got = top_by_priority(rows, sims, k, **args)
            want = _definition_top(rows, sims, k, min_sim, 1200.0, args["half_life_days"], weights)
            with self.subTest(trial=trial):
                self.assertEqual([(s, r["id"]) for s, r in got], [(s, r["id"]) for s, r in want])

    def test_negative_weights_are_refused(self):
        with self.assertRaises(ValueError):
            top_by_priority([], [], 3, min_sim=0.0, now=0.0, half_life_days=30.0, weights=(1.0, -0.1, 0.0))


def _definition_fuse(channels):
    """fuse() as it was: a Fused-like entry per candidate, accumulated in channel order."""
    accum: dict[str, list] = {}
    for channel in channels:
        weight = DEFAULT_WEIGHTS.get(channel.name, 1.0)
        for rank_0, fact_id in enumerate(channel.ranked_ids):
            contribution = weight / (DEFAULT_SMOOTHING + rank_0 + 1)
            entry = accum.setdefault(fact_id, [0.0, {}])
            entry[0] += contribution
            entry[1][channel.name] = contribution
    ordered = sorted(accum.items(), key=lambda item: item[1][0], reverse=True)
    return [(fid, score, contributions) for fid, (score, contributions) in ordered]


class FuseLimitTests(unittest.TestCase):
    def test_full_and_limited_fusion_equal_the_original_definition(self):
        rng = random.Random(5)
        names = list(DEFAULT_WEIGHTS) + ["unknown"]
        for _ in range(200):
            ids = [f"f{i}" for i in range(rng.randint(0, 40))]
            channels = [
                Channel(name, rng.sample(ids, rng.randint(0, len(ids))))
                for name in rng.sample(names, rng.randint(1, 6))
            ]
            want = _definition_fuse(channels)
            full = [(f.fact_id, f.score, f.contributions) for f in fuse(channels)]
            self.assertEqual(full, want)
            for limit in (0, 1, 3, 100):
                limited = [(f.fact_id, f.score, f.contributions) for f in fuse(channels, limit=limit)]
                self.assertEqual(limited, want[:limit])


class ScanRowsTests(unittest.TestCase):
    def setUp(self):
        temp_data_dir(self)
        self.cfg = replace(get_config(), distiller="heuristic")
        self.store = Store(self.cfg.db_path)
        self.addCleanup(self.store.close)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}
        texts = [
            "The deploy pipeline runs on GitHub Actions and ships to AWS Lambda.",
            "Recall injects facts through additionalContext on UserPromptSubmit.",
            "Embeddings are quantised to int8 so the store stays compact.",
            "The capture worker runs detached so a turn never waits on distillation.",
            "Consolidation runs at SessionEnd and PreCompact, like sleep.",
            "The viewer is a stdlib http.server over the Store repository.",
            "Supersession archives an older near-duplicate fact, reversibly.",
            "Project identity hashes the workspace root into a collision-free key.",
        ]
        for text in texts:
            service.add_facts(self.store, self.embedder, self.cfg, self.project, "s1", [text])
        self.assertEqual(len(self.store.active_rows_for_project("p")), len(texts))  # distinct: none superseded
        glob = {"key": GLOBAL_PROJECT_KEY, "path": "", "label": "global"}
        service.add_records(
            self.store,
            self.embedder,
            self.cfg,
            glob,
            "s1",
            [DistilledFact("never run rm -rf on the data dir", type="antipattern")],
            kind="antipattern",
        )

    def test_lean_rows_match_the_full_query_in_order_and_values(self):
        full = self.store.active_rows_for_project("p")
        lean = self.store.scan_rows("p", text=True)
        self.assertEqual([r["id"] for r in lean], [r["id"] for r in full])
        self.assertEqual(
            set(lean[0].keys()),
            {"id", "dim", "scale", "vec_int8", "created_at", "last_seen", "frequency", "tier", "text"},
        )
        for a, b in zip(lean, full):
            self.assertTrue(all(a[c] == b[c] for c in a.keys()))
        self.assertNotIn("text", self.store.scan_rows("p")[0].keys())

    def test_kind_filter_and_the_global_anti_pattern_union(self):
        self.assertEqual(len(self.store.scan_rows(GLOBAL_PROJECT_KEY, kind="antipattern")), 1)
        self.assertEqual(len(self.store.scan_rows(GLOBAL_PROJECT_KEY, kind="fact")), 0)
        self.assertEqual(len(_recall_rows(self.store, "p")), len(self.store.active_rows_for_project("p")) + 1)

    def test_shipped_paths_rank_like_the_full_definition_and_return_full_rows(self):
        cfg = replace(self.cfg, min_sim=-1.0, top_k=4, activated_k=4)
        query = "deploy pipelines int8"
        hits = search(self.store, self.embedder, self.project, query, cfg, now=10**10)
        rows = _recall_rows(self.store, "p")
        sims = get_scorer(cfg).cosine_all(rows, self.embedder.embed_query(query))
        want = sorted(_score(rows, sims, cfg, 10**10, cfg.min_sim, 1.0), key=lambda h: h[0], reverse=True)[:4]
        self.assertEqual([(s, r["id"]) for s, r in hits], [(s, r["id"]) for s, r in want])
        fused = search_fused_with_stats(self.store, self.embedder, self.project, query, cfg, k=4).hits
        for row in [r for _s, r in hits] + [r for _f, _s, r in fused]:
            self.assertIn("narrative", row.keys())  # hydrated: callers read every column

    def test_hydrate_drops_a_fact_deleted_after_the_scan(self):
        lean = self.store.scan_rows("p")[:2]
        self.store.delete_facts([lean[0]["id"]])
        hydrated = _hydrate(self.store, [(1.0, lean[0]), (0.5, lean[1])])
        self.assertEqual([(s, r["id"]) for s, r in hydrated], [(0.5, lean[1]["id"])])


if __name__ == "__main__":
    unittest.main()
