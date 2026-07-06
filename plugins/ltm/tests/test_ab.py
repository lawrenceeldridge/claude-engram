"""Known-value tests for the pure parts of the A/B harness (bench/run_ab.py).

The runner itself needs the `claude` CLI and real tokens; only its statistics
and accounting are testable here — and they must be exactly right, since they
back the headline savings claim.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench.run_ab import composite_tokens, wilcoxon_exact  # noqa: E402


class CompositeTokenTests(unittest.TestCase):
    def test_price_ratio_weights(self):
        usage = {
            "input_tokens": 100,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 100,
            "output_tokens": 100,
        }
        # 100*1.0 + 100*1.25 + 100*0.1 + 100*1.0
        self.assertAlmostEqual(composite_tokens(usage), 335.0)

    def test_missing_fields_default_to_zero(self):
        self.assertEqual(composite_tokens({}), 0.0)


class WilcoxonTests(unittest.TestCase):
    def test_all_positive_n5_known_value(self):
        # W- = 0; exact two-sided p = 2 * 1/2^5 = 0.0625
        w_minus, p = wilcoxon_exact([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual(w_minus, 0.0)
        self.assertAlmostEqual(p, 0.0625, places=10)

    def test_zeros_are_dropped(self):
        w_minus, p = wilcoxon_exact([0.0, 0.0, 1.0])
        self.assertEqual(w_minus, 0.0)
        self.assertEqual(p, 1.0)  # n=1 after dropping zeros: 2 * 1/2 = 1.0

    def test_empty_and_all_zero(self):
        self.assertEqual(wilcoxon_exact([]), (0.0, 1.0))
        self.assertEqual(wilcoxon_exact([0.0, 0.0]), (0.0, 1.0))

    def test_symmetric_diffs_not_significant(self):
        _w, p = wilcoxon_exact([1.0, -1.0])
        self.assertEqual(p, 1.0)

    def test_sign_flip_symmetry(self):
        diffs = [3.0, -1.0, 4.0, 2.0, -2.5, 5.0]
        _w1, p1 = wilcoxon_exact(diffs)
        _w2, p2 = wilcoxon_exact([-d for d in diffs])
        self.assertAlmostEqual(p1, p2, places=10)

    def test_strong_effect_is_significant(self):
        # 10 consistent positive diffs: p = 2/2^10 < 0.01
        _w, p = wilcoxon_exact([float(i) for i in range(1, 11)])
        self.assertLess(p, 0.01)


if __name__ == "__main__":
    unittest.main()
