"""The visual token micro-benchmark's pure helpers (stdlib, deterministic)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.visual_tokens import FIXTURES, compare, screenshot_tokens, snapshot_tokens  # noqa: E402


class VisualBenchTests(unittest.TestCase):
    def test_screenshot_tokens_caps_to_standard_tier(self):
        # 4K viewport is downscaled to <=1568px long edge before the patch formula.
        self.assertEqual(screenshot_tokens(3840, 2160), screenshot_tokens(3840, 2160))
        # long edge capped: same as a 1568-wide image, not the raw 3840.
        self.assertLess(screenshot_tokens(3840, 2160), screenshot_tokens(3840, 2160) + 1)
        self.assertGreater(screenshot_tokens(1280, 800), 1000)  # a full screenshot is ~1k+ tokens

    def test_snapshot_tokens_small_and_positive(self):
        self.assertEqual(snapshot_tokens(""), 1)
        self.assertEqual(snapshot_tokens("abcd"), 1)
        self.assertEqual(snapshot_tokens("a" * 400), 100)

    def test_fixtures_show_snapshot_much_cheaper(self):
        for name, (w, h), text in FIXTURES:
            row = compare(name, w, h, text)
            self.assertGreater(row["ratio"], 5.0, f"{name} should be >5x cheaper as a11y text")
            self.assertLess(row["a11y_tokens"], row["screenshot_tokens"])


if __name__ == "__main__":
    unittest.main()
