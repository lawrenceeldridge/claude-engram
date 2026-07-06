"""Matryoshka truncation helper (core/adapters/fastembed_gw.truncate_renorm).

The helper is pure and module-level, so it tests on the stdlib without
fastembed installed — the adapter only imports fastembed inside __init__.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.adapters.fastembed_gw import truncate_renorm  # noqa: E402


class TruncateRenormTests(unittest.TestCase):
    def test_zero_is_noop(self):
        vec = [0.6, 0.8, 0.0]
        self.assertEqual(truncate_renorm(vec, 0), vec)

    def test_dim_at_or_above_native_is_noop(self):
        vec = [0.6, 0.8]
        self.assertEqual(truncate_renorm(vec, 2), vec)
        self.assertEqual(truncate_renorm(vec, 5), vec)

    def test_truncates_and_renormalises(self):
        out = truncate_renorm([3.0, 4.0, 100.0, -7.0], 2)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(math.hypot(*out), 1.0, places=12)
        self.assertAlmostEqual(out[0], 0.6)
        self.assertAlmostEqual(out[1], 0.8)

    def test_zero_head_does_not_divide_by_zero(self):
        out = truncate_renorm([0.0, 0.0, 1.0], 2)
        self.assertEqual(out, [0.0, 0.0])

    def test_helper_usable_without_gateway(self):
        # The helper is module-level and pure — usable without constructing
        # FastEmbedGateway (whose __init__ is where fastembed actually imports).
        self.assertEqual(truncate_renorm([1.0], 0), [1.0])


if __name__ == "__main__":
    unittest.main()
