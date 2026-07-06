"""Known-value tests for the bench harness statistics (wilson / mcnemar / bootstrap).

These functions back the paired-comparison output of `engram eval`; a bug here
becomes a false claim in a design doc, so each is pinned to hand-computed
values from worked examples.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench.run_eval import _score_queries, bootstrap_ci, mcnemar_exact, wilson  # noqa: E402


class McNemarTests(unittest.TestCase):
    def test_known_value_1_vs_9(self):
        # n=10 discordant, min side 1: p = 2 * (C(10,0)+C(10,1)) / 2^10 = 22/1024
        self.assertAlmostEqual(mcnemar_exact(1, 9), 22 / 1024, places=10)

    def test_no_discordant_pairs_is_one(self):
        self.assertEqual(mcnemar_exact(0, 0), 1.0)

    def test_balanced_discordance_caps_at_one(self):
        # b == c: the doubled tail exceeds 1 and must be capped, never > 1.
        self.assertEqual(mcnemar_exact(5, 5), 1.0)

    def test_symmetric(self):
        self.assertEqual(mcnemar_exact(3, 7), mcnemar_exact(7, 3))

    def test_large_imbalance_is_significant(self):
        self.assertLess(mcnemar_exact(0, 10), 0.05)


class BootstrapTests(unittest.TestCase):
    def test_constant_deltas_zero_width(self):
        lo, hi = bootstrap_ci([0.5] * 20)
        self.assertEqual((lo, hi), (0.5, 0.5))

    def test_empty_is_zero(self):
        self.assertEqual(bootstrap_ci([]), (0.0, 0.0))

    def test_seeded_and_deterministic(self):
        deltas = [0.1, -0.2, 0.3, 0.0, 0.25, -0.05]
        self.assertEqual(bootstrap_ci(deltas, seed=0), bootstrap_ci(deltas, seed=0))
        lo, hi = bootstrap_ci(deltas)
        self.assertLess(lo, hi)

    def test_interval_brackets_the_mean(self):
        deltas = [1.0, 2.0, 3.0, 4.0]
        lo, hi = bootstrap_ci(deltas)
        self.assertLessEqual(lo, 2.5)
        self.assertGreaterEqual(hi, 2.5)


class WilsonTests(unittest.TestCase):
    def test_known_value_half(self):
        # k=50, n=100 -> p=0.5, 95% Wilson interval ~ [0.404, 0.596]
        lo, hi = wilson(50, 100)
        self.assertAlmostEqual(lo, 0.404, places=3)
        self.assertAlmostEqual(hi, 0.596, places=3)

    def test_zero_n(self):
        self.assertEqual(wilson(0, 0), (0.0, 0.0))

    def test_bounded(self):
        lo, hi = wilson(0, 10)
        self.assertGreaterEqual(lo, 0.0)
        lo, hi = wilson(10, 10)
        self.assertLessEqual(hi, 1.0)


class PerQueryTests(unittest.TestCase):
    def test_per_query_consistent_with_aggregates(self):
        facts = ["alpha fact", "beta fact", "gamma fact"]
        queries = [
            {"q": "find alpha", "relevant": [0]},  # ranked first -> hit1
            {"q": "find beta", "relevant": [1]},  # ranked second -> hit3, rr=0.5
            {"q": "find gamma", "relevant": [2]},  # never ranked -> miss
        ]
        ranked_by_query = {
            "find alpha": ["alpha fact", "beta fact"],
            "find beta": ["alpha fact", "beta fact"],
            "find gamma": ["alpha fact", "beta fact"],
        }
        r1, r3, mrr, _ms, per_query = _score_queries(queries, facts, lambda q: ranked_by_query[q])
        self.assertAlmostEqual(r1, sum(p["hit1"] for p in per_query) / 3)
        self.assertAlmostEqual(r3, sum(p["hit3"] for p in per_query) / 3)
        self.assertAlmostEqual(mrr, sum(p["rr"] for p in per_query) / 3)
        self.assertEqual([p["hit1"] for p in per_query], [True, False, False])
        self.assertEqual([p["rr"] for p in per_query], [1.0, 0.5, 0.0])


if __name__ == "__main__":
    unittest.main()
