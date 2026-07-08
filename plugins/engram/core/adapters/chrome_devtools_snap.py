"""Chrome DevTools snapshotter — attaches over CDP to an already-running Chrome.

'Chrome DevTools' here means the Chrome DevTools Protocol: this adapter connects
to a Chrome started with ``--remote-debugging-port`` (the caller's browser) and
reads its accessibility snapshot, rather than launching its own. Playwright is
used purely as the CDP transport — the stdlib has no websocket client — so a
dependency-free raw-CDP transport could replace it later without touching the
core. Imported lazily; fails open to an empty PageView.
"""

from __future__ import annotations

import sys

from core.adapters._snapshot_util import view_from_page
from core.ports.snapshot import PageView, SnapshotGateway


class ChromeDevToolsSnapshotter(SnapshotGateway):
    """Attaches to an existing Chrome over CDP and returns the active page's a11y snapshot."""

    def __init__(self, cdp_url: str = "http://localhost:9222", timeout_ms: int = 5000) -> None:
        self._cdp_url = cdp_url
        self._timeout_ms = timeout_ms

    def snapshot(self, target: str | None = None) -> PageView:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # CDP transport absent — fail open
            print(f"[engram] playwright (CDP transport) unavailable ({exc}); empty snapshot", file=sys.stderr)
            return PageView(text="")
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(self._cdp_url, timeout=self._timeout_ms)
                try:
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_default_timeout(self._timeout_ms)
                    if target:
                        page.goto(target, wait_until="load")
                    return view_from_page(page)
                finally:
                    browser.close()
        except Exception as exc:  # no Chrome at cdp_url / attach failure — fail open
            print(
                f"[engram] chrome-devtools (CDP {self._cdp_url}) snapshot failed ({exc}); empty snapshot",
                file=sys.stderr,
            )
            return PageView(text="")
