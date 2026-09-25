"""Known-value tests for the bench harness statistics (``bench/stats.py``).

These functions back the paired-comparison and calibration output of `engram eval`; a bug here
becomes a false claim in a design doc, so each is pinned to hand-computed
values from worked examples.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench.retrieval import score_queries  # noqa: E402
from bench.stats import (  # noqa: E402
    auroc,
    bootstrap_ci,
    bootstrap_stat_ci,
    brier,
    ece,
    mcnemar_exact,
    ndcg_at_k,
    platt_apply,
    platt_fit,
    recall_all_at_k,
    recall_any_at_k,
    reliability_bins,
    wilson,
)


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
        r1, r3, mrr, _ms, per_query = score_queries(queries, facts, lambda q: ranked_by_query[q])
        self.assertAlmostEqual(r1, sum(p["hit1"] for p in per_query) / 3)
        self.assertAlmostEqual(r3, sum(p["hit3"] for p in per_query) / 3)
        self.assertAlmostEqual(mrr, sum(p["rr"] for p in per_query) / 3)
        self.assertEqual([p["hit1"] for p in per_query], [True, False, False])
        self.assertEqual([p["rr"] for p in per_query], [1.0, 0.5, 0.0])


class BootstrapStatTests(unittest.TestCase):
    def test_undefined_draws_are_skipped_not_guessed(self):
        self.assertEqual(bootstrap_stat_ci(5, lambda idx: None), (0.0, 0.0))

    def test_matches_bootstrap_ci_for_the_mean(self):
        deltas = [0.1, -0.2, 0.3, 0.0, 0.25, -0.05]
        mean = bootstrap_stat_ci(len(deltas), lambda idx: sum(deltas[i] for i in idx) / len(deltas))
        self.assertEqual(mean, bootstrap_ci(deltas))


class AurocTests(unittest.TestCase):
    def test_known_value(self):
        # pos {0.35, 0.8} vs neg {0.1, 0.4}: 3 of 4 pairs ordered correctly.
        self.assertAlmostEqual(auroc([0.1, 0.4, 0.35, 0.8], [False, False, True, True]), 0.75)

    def test_perfect_and_inverted(self):
        self.assertEqual(auroc([0.1, 0.9], [False, True]), 1.0)
        self.assertEqual(auroc([0.9, 0.1], [False, True]), 0.0)

    def test_ties_count_half(self):
        self.assertEqual(auroc([0.5, 0.5], [True, False]), 0.5)
        self.assertAlmostEqual(auroc([0.2, 0.5, 0.5, 0.9], [False, False, True, True]), 0.875)

    def test_single_class_is_undefined(self):
        self.assertIsNone(auroc([0.1, 0.2], [True, True]))
        self.assertIsNone(auroc([], []))

    def test_invariant_to_monotone_rescaling(self):
        scores, labels = [0.1, 0.7, 0.3, 0.9, 0.5], [False, True, False, True, True]
        self.assertEqual(auroc(scores, labels), auroc([10 * s - 3 for s in scores], labels))

    def test_negative_infinity_ranks_lowest(self):
        self.assertEqual(auroc([float("-inf"), 0.2], [False, True]), 1.0)


class CalibrationTests(unittest.TestCase):
    def test_brier_known_values(self):
        self.assertEqual(brier([1.0, 0.0], [True, False]), 0.0)
        self.assertEqual(brier([0.5, 0.5], [True, False]), 0.25)
        self.assertEqual(brier([], []), 0.0)

    def test_ece_zero_when_calibrated(self):
        self.assertAlmostEqual(ece([0.9] * 10, [True] * 9 + [False]), 0.0)

    def test_ece_known_miscalibration(self):
        self.assertAlmostEqual(ece([0.9] * 10, [True] * 5 + [False] * 5), 0.4)

    def test_reliability_bins_clamp_edges(self):
        bins = reliability_bins([0.0, 1.0], [False, True])
        self.assertEqual([(b["lo"], b["n"]) for b in bins], [(0.0, 1), (0.9, 1)])


class PlattTests(unittest.TestCase):
    def test_symmetric_known_value(self):
        # Smoothed targets 3/4 and 1/4 at s=+1/-1: the optimum is b=0, sigmoid(a)=3/4, so a=ln 3.
        a, b = platt_fit([-1.0, -1.0, 1.0, 1.0], [False, False, True, True])
        self.assertAlmostEqual(a, math.log(3), places=6)
        self.assertAlmostEqual(b, 0.0, places=6)

    def test_separable_sample_stays_finite_and_monotone(self):
        params = platt_fit([0.1, 0.2, 0.8, 0.9], [False, False, True, True])
        self.assertTrue(all(math.isfinite(v) for v in params))
        probs = [platt_apply(s, params) for s in (0.1, 0.5, 0.9)]
        self.assertEqual(probs, sorted(probs))
        self.assertLess(probs[0], 0.5)
        self.assertGreater(probs[2], 0.5)

    def test_no_recall_maps_to_zero(self):
        self.assertEqual(platt_apply(float("-inf"), (5.0, -1.0)), 0.0)


class RetrievalMetricTests(unittest.TestCase):
    def test_recall_any_and_all(self):
        ranked = ["a", "b", "c", "d"]
        self.assertTrue(recall_any_at_k(ranked, {"c", "z"}, 3))
        self.assertFalse(recall_any_at_k(ranked, {"d"}, 3))
        self.assertTrue(recall_all_at_k(ranked, {"a", "c"}, 3))
        self.assertFalse(recall_all_at_k(ranked, {"a", "d"}, 3))
        self.assertFalse(recall_all_at_k(ranked, set(), 3))  # no gold: undefined, never a hit

    def test_ndcg_known_values(self):
        self.assertAlmostEqual(ndcg_at_k(["g", "x"], {"g"}, 5), 1.0)
        self.assertAlmostEqual(ndcg_at_k(["x", "g"], {"g"}, 5), 1 / math.log2(3))
        # two gold at ranks 1 and 3: (1 + 1/log2 4) / (1 + 1/log2 3)
        self.assertAlmostEqual(ndcg_at_k(["g1", "x", "g2"], {"g1", "g2"}, 5), (1 + 0.5) / (1 + 1 / math.log2(3)))
        self.assertEqual(ndcg_at_k(["x"], set(), 5), 0.0)


if __name__ == "__main__":
    unittest.main()
