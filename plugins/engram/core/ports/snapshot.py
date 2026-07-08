"""Snapshot gateway (port) + a dependency-free stub adapter.

Ports & Adapters, mirroring ``core/ports/embedding.py``: the model-invoked
``compact_page_view`` MCP tool reads a page through a ``SnapshotGateway`` without
the core importing any browser or MCP client. Real backends (Chrome DevTools MCP,
Playwright) are driven adapters under ``core/adapters/`` selected by
``get_snapshotter``; the zero-dependency ``StubSnapshotter`` is both the default
and the test fake — the analogue of ``HashEmbedding`` for the snapshot seam.

v1 exposes the accessibility-tree TEXT snapshot only — the token-cheapest way to
let the model read a page (see the ``visual-token-reduction-research`` memory).
The pixel path (screenshots / visual diff) is deferred to a v2 follow-up.
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class PageView:
    """Immutable value object: a text (accessibility-tree) view of a page.

    ``text`` is the a11y snapshot the model reads. ``width`` / ``height`` are the
    optional viewport dimensions (used by the token micro-benchmark and the
    deferred v2 pixel path). An empty ``text`` is the Null-Object 'nothing to
    show' case — the tool DTO renders it as ``""``.
    """

    text: str
    url: str | None = None
    width: int | None = None
    height: int | None = None

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class SnapshotGateway(ABC):
    """Port: a source of a page's accessibility-tree text snapshot."""

    @abstractmethod
    def snapshot(self) -> PageView: ...


_CANNED = """\
document "Example — Sign in"
  banner
    link "Home" [ref=e1]
  main
    heading "Sign in" level=1
    textbox "Email" [ref=e2]
    textbox "Password" [ref=e3]
    button "Sign in" [ref=e4]
  contentinfo
    text "© Example"
"""


class StubSnapshotter(SnapshotGateway):
    """Deterministic, dependency-free stub: returns a canned a11y-tree snapshot.

    The zero-dep default (no browser) and the test fake.
    """

    def __init__(self, text: str = _CANNED, url: str = "about:blank") -> None:
        self._text = text
        self._url = url

    def snapshot(self) -> PageView:
        return PageView(text=self._text, url=self._url)


def get_snapshotter(cfg) -> SnapshotGateway:
    """Plugin selection: pick the snapshot backend from config, fail-open to the stub.

    ``cfg.snapshotter`` = 'chrome-devtools' | 'playwright' | 'stub' (default). Any
    missing dependency, dead browser, or unknown backend degrades to the stub so
    the tool never raises — mirrors ``get_embedder``'s fastembed->hash fallback.
    """
    backend = getattr(cfg, "snapshotter", "stub")
    if backend == "chrome-devtools":
        try:
            from core.adapters.chrome_devtools_snap import ChromeDevToolsSnapshotter

            return ChromeDevToolsSnapshotter()
        except Exception as exc:  # fail-open — never break the tool
            print(f"[engram] chrome-devtools snapshotter unavailable ({exc}); using stub", file=sys.stderr)
    elif backend == "playwright":
        try:
            from core.adapters.playwright_snap import PlaywrightSnapshotter

            return PlaywrightSnapshotter()
        except Exception as exc:  # fail-open
            print(f"[engram] playwright snapshotter unavailable ({exc}); using stub", file=sys.stderr)
    return StubSnapshotter()
