"""Playwright snapshotter — launches its own chromium and returns the a11y snapshot.

Optional driven adapter (Ports & Adapters): ``playwright`` is imported lazily
inside ``snapshot`` so the core imports cleanly without it. Any failure (missing
dependency, no browser installed, navigation error) fails open to an empty
PageView — it never raises into the tool. Text-only (a11y) in v1; screenshots are
deferred to v2.
"""

from __future__ import annotations

import sys

from core.adapters._snapshot_util import view_from_page
from core.ports.snapshot import PageView, SnapshotGateway


class PlaywrightSnapshotter(SnapshotGateway):
    """Launches a headless chromium, navigates to ``target``, returns its a11y snapshot."""

    def __init__(self, headless: bool = True, timeout_ms: int = 5000) -> None:
        self._headless = headless
        self._timeout_ms = timeout_ms

    def snapshot(self, target: str | None = None) -> PageView:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # dependency absent — fail open
            print(f"[engram] playwright unavailable ({exc}); empty snapshot", file=sys.stderr)
            return PageView(text="")
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self._headless)
                try:
                    page = browser.new_page()
                    page.set_default_timeout(self._timeout_ms)
                    if target:
                        page.goto(target, wait_until="load")
                    return view_from_page(page)
                finally:
                    browser.close()
        except Exception as exc:  # no browser / navigation failure — fail open
            print(f"[engram] playwright snapshot failed ({exc}); empty snapshot", file=sys.stderr)
            return PageView(text="")
