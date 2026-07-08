"""Browser-backed snapshotters — shared mapping (fake page), fail-open, and a live
snapshot that self-skips when no working Playwright browser is present.

Design notes:
- ``view_from_page`` is duck-typed, so the mapping is validated with a fake page (no browser).
- Fail-open is validated with *unreachable* targets (port 1), so the assertion holds whether
  or not a browser can launch — a failed browser snapshot must degrade to an empty PageView.
- The live test uses a ``data:`` URL (no network) and skips unless chromium actually launches.
"""

from __future__ import annotations

import unittest

from core.adapters._snapshot_util import view_from_page
from core.adapters.chrome_devtools_snap import ChromeDevToolsSnapshotter
from core.adapters.playwright_snap import PlaywrightSnapshotter
from core.ports.snapshot import PageView


class _FakeLocator:
    def __init__(self, yaml: str) -> None:
        self._yaml = yaml

    def aria_snapshot(self, **_kw) -> str:
        return self._yaml


class _FakePage:
    def __init__(self, yaml: str, url, viewport) -> None:
        self._yaml = yaml
        self.url = url
        self.viewport_size = viewport

    def locator(self, _selector: str) -> _FakeLocator:
        return _FakeLocator(self._yaml)


class ViewFromPageTests(unittest.TestCase):
    def test_maps_aria_snapshot_to_pageview(self):
        page = _FakePage(
            '- heading "Sign in"\n- button "Go"',
            "https://example.com",
            {"width": 1024, "height": 768},
        )
        view = view_from_page(page)
        self.assertIsInstance(view, PageView)
        self.assertIn("Sign in", view.text)
        self.assertEqual(view.url, "https://example.com")
        self.assertEqual((view.width, view.height), (1024, 768))
        self.assertFalse(view.is_empty)

    def test_empty_tree_and_missing_viewport(self):
        view = view_from_page(_FakePage("", None, None))
        self.assertTrue(view.is_empty)
        self.assertIsNone(view.width)
        self.assertIsNone(view.height)


class FailOpenTests(unittest.TestCase):
    def test_playwright_fails_open_to_empty(self):
        # Unreachable target (port 1): launch may or may not succeed, but the snapshot
        # cannot complete, so it must degrade to an empty PageView without raising.
        view = PlaywrightSnapshotter(timeout_ms=1500).snapshot("http://127.0.0.1:1/")
        self.assertIsInstance(view, PageView)
        self.assertTrue(view.is_empty)

    def test_chrome_devtools_fails_open_to_empty(self):
        # Nothing listening on the CDP port -> attach fails -> empty PageView, no raise.
        view = ChromeDevToolsSnapshotter(cdp_url="http://127.0.0.1:1", timeout_ms=1500).snapshot()
        self.assertIsInstance(view, PageView)
        self.assertTrue(view.is_empty)


class LiveSnapshotTests(unittest.TestCase):
    def test_live_playwright_snapshot(self):
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                pw.chromium.launch(headless=True).close()
        except Exception as exc:  # no dep / browser build mismatch -> skip, don't fail
            self.skipTest(f"no working Playwright browser: {exc}")
        view = PlaywrightSnapshotter().snapshot("data:text/html,<h1>Sign in</h1><button>Go</button>")
        self.assertFalse(view.is_empty)
        self.assertIn("Sign in", view.text)


if __name__ == "__main__":
    unittest.main()
