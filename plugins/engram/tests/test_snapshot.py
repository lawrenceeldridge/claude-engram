"""SnapshotGateway port + stub + Plugin selection (fail-open) + core-imports-clean."""

from __future__ import annotations

import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from core.config import get_config
from core.ports.snapshot import (
    PageView,
    SnapshotGateway,
    StubSnapshotter,
    get_snapshotter,
    render_page_view,
)

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent  # plugins/engram


class StubSnapshotterTests(unittest.TestCase):
    def test_returns_canned_a11y_text(self):
        view = StubSnapshotter().snapshot()
        self.assertIsInstance(view, PageView)
        self.assertIn("Sign in", view.text)
        self.assertFalse(view.is_empty)

    def test_is_a_gateway(self):
        self.assertIsInstance(StubSnapshotter(), SnapshotGateway)

    def test_custom_text(self):
        view = StubSnapshotter(text="heading 'X'").snapshot()
        self.assertEqual(view.text, "heading 'X'")


class PageViewTests(unittest.TestCase):
    def test_empty_is_null_object(self):
        self.assertTrue(PageView(text="").is_empty)
        self.assertTrue(PageView(text="   \n").is_empty)
        self.assertFalse(PageView(text="a").is_empty)

    def test_is_frozen(self):
        view = PageView(text="a")
        with self.assertRaises(Exception):
            view.text = "b"  # type: ignore[misc]


class GetSnapshotterTests(unittest.TestCase):
    def test_defaults_to_stub(self):
        self.assertIsInstance(get_snapshotter(get_config()), StubSnapshotter)

    def test_stub_selected_explicitly(self):
        cfg = replace(get_config(), snapshotter="stub")
        self.assertIsInstance(get_snapshotter(cfg), StubSnapshotter)

    def test_unknown_backend_fails_open_to_stub(self):
        cfg = replace(get_config(), snapshotter="does-not-exist")
        self.assertIsInstance(get_snapshotter(cfg), StubSnapshotter)

    def test_playwright_backend_selected(self):
        from core.adapters.playwright_snap import PlaywrightSnapshotter

        cfg = replace(get_config(), snapshotter="playwright")
        self.assertIsInstance(get_snapshotter(cfg), PlaywrightSnapshotter)

    def test_chrome_devtools_backend_selected(self):
        from core.adapters.chrome_devtools_snap import ChromeDevToolsSnapshotter

        cfg = replace(get_config(), snapshotter="chrome-devtools", snapshot_cdp_url="http://localhost:9222")
        self.assertIsInstance(get_snapshotter(cfg), ChromeDevToolsSnapshotter)


class CoreImportsCleanTests(unittest.TestCase):
    def test_imports_clean_in_fresh_interpreter(self):
        # A fresh interpreter importing only the port + pure budget pulls in no heavy
        # dep (no Pillow, no fastembed) — the stdlib-first-core guarantee for this seam.
        code = (
            "import sys; "
            "import core.ports.snapshot, core.domain.visual_budget; "
            "import core.adapters.playwright_snap, core.adapters.chrome_devtools_snap, "
            "core.adapters._snapshot_util; "
            "assert 'PIL' not in sys.modules, 'PIL leaked'; "
            "assert 'fastembed' not in sys.modules, 'fastembed leaked'; "
            "assert 'playwright' not in sys.modules, 'playwright leaked at import'"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_PLUGIN_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class RenderPageViewTests(unittest.TestCase):
    def test_non_empty_uncapped(self):
        dto = render_page_view(PageView(text="heading X", url="u"), max_chars=1000)
        self.assertFalse(dto["empty"])
        self.assertFalse(dto["truncated"])
        self.assertEqual(dto["text"], "heading X")
        self.assertEqual(dto["url"], "u")
        self.assertEqual(dto["chars"], len("heading X"))

    def test_empty_is_null_object(self):
        dto = render_page_view(PageView(text="   \n"), max_chars=1000)
        self.assertTrue(dto["empty"])
        self.assertEqual(dto["text"], "")
        self.assertEqual(dto["chars"], 0)
        self.assertFalse(dto["truncated"])

    def test_caps_at_max_chars(self):
        dto = render_page_view(PageView(text="a" * 500), max_chars=100)
        self.assertTrue(dto["truncated"])
        self.assertIn("[truncated]", dto["text"])
        self.assertLessEqual(len(dto["text"]), 100 + len("\n… [truncated]"))

    def test_zero_max_chars_disables_cap(self):
        dto = render_page_view(PageView(text="abc"), max_chars=0)
        self.assertFalse(dto["truncated"])
        self.assertEqual(dto["text"], "abc")


if __name__ == "__main__":
    unittest.main()
