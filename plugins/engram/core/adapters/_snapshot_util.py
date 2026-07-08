"""Shared page -> PageView mapping for the browser-backed snapshotters.

Duck-typed over a Playwright ``Page`` — there is deliberately **no** ``playwright``
import here, so this module stays import-clean and unit-testable with a fake page.
The browser library is imported lazily inside each adapter's ``snapshot`` method.
"""

from __future__ import annotations

from core.ports.snapshot import PageView


def view_from_page(page) -> PageView:
    """Build a PageView from a live page via its accessibility (ARIA) snapshot.

    Uses ``locator("body").aria_snapshot()`` — the YAML accessibility-tree text
    that is the token-cheap, recommended representation (Playwright's
    ``browser_snapshot`` equivalent). An empty tree yields an empty PageView
    (the Null-Object 'nothing to show' case).
    """
    text = page.locator("body").aria_snapshot() or ""
    viewport = page.viewport_size or {}
    return PageView(
        text=text,
        url=getattr(page, "url", None),
        width=viewport.get("width"),
        height=viewport.get("height"),
    )
