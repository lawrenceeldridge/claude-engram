"""Pure visual-budget arithmetic — token estimate + tier downscale (stdlib, no mocks)."""

from __future__ import annotations

import unittest

from core.domain import visual_budget as vb


class EstimateVisualTokensTests(unittest.TestCase):
    def test_documented_patch_grid_values(self):
        # Mirrors Anthropic's documented table (28x28-px patches).
        self.assertEqual(vb.estimate_visual_tokens(200, 200), 64)
        self.assertEqual(vb.estimate_visual_tokens(1000, 1000), 1296)
        self.assertEqual(vb.estimate_visual_tokens(1092, 1092), 1521)

    def test_ceil_not_floor(self):
        # 29 px spans two 28-px patches on each axis.
        self.assertEqual(vb.estimate_visual_tokens(29, 29), 4)

    def test_nonpositive_is_zero(self):
        self.assertEqual(vb.estimate_visual_tokens(0, 500), 0)
        self.assertEqual(vb.estimate_visual_tokens(500, -1), 0)


class DownscaleToTierTests(unittest.TestCase):
    def test_noop_when_within_tier(self):
        self.assertEqual(vb.downscale_to_tier(800, 600), (800, 600))
        self.assertEqual(vb.downscale_to_tier(1568, 1000), (1568, 1000))

    def test_caps_long_edge_and_preserves_aspect(self):
        w, h = vb.downscale_to_tier(3840, 2160)
        self.assertEqual(max(w, h), 1568)
        self.assertAlmostEqual(w / h, 3840 / 2160, places=1)

    def test_never_upscales(self):
        self.assertEqual(vb.downscale_to_tier(100, 50), (100, 50))

    def test_custom_tier(self):
        w, h = vb.downscale_to_tier(4000, 2000, tier_px=1000)
        self.assertEqual(max(w, h), 1000)


if __name__ == "__main__":
    unittest.main()
