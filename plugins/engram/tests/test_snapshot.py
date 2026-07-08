"""SnapshotGateway port + stub + Plugin selection (fail-open) + core-imports-clean."""

from __future__ import annotations

import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from core.config import get_config
from core.ports.snapshot import PageView, SnapshotGateway, StubSnapshotter, get_snapshotter

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

    def test_unavailable_adapter_fails_open_to_stub(self):
        # Adapters are not built yet (v1 Phase 3): selecting one must degrade to the stub.
        cfg = replace(get_config(), snapshotter="chrome-devtools")
        self.assertIsInstance(get_snapshotter(cfg), StubSnapshotter)


class CoreImportsCleanTests(unittest.TestCase):
    def test_imports_clean_in_fresh_interpreter(self):
        # A fresh interpreter importing only the port + pure budget pulls in no heavy
        # dep (no Pillow, no fastembed) — the stdlib-first-core guarantee for this seam.
        code = (
            "import sys; import core.ports.snapshot, core.domain.visual_budget; "
            "assert 'PIL' not in sys.modules, 'PIL leaked'; "
            "assert 'fastembed' not in sys.modules, 'fastembed leaked'"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_PLUGIN_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
