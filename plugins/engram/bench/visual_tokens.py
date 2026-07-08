#!/usr/bin/env python3
"""Token micro-benchmark: accessibility-tree snapshot vs screenshot.

Quantifies the visual-snapshot-compaction thesis — reading a page as a11y TEXT costs
far fewer tokens than a screenshot of the same page. Offline and deterministic by
default (canned fixtures + the pure token formula); pass ``--url`` to snapshot a live
page via the configured browser backend.

This is NOT ``engram eval``: the feature touches no embedding / ranking / quantisation /
fusion / distillation, so the recall benchmark does not apply. This is its own measure.

- Screenshot cost = Claude's patch formula ``ceil(w/28)*ceil(h/28)`` after capping the
  long edge to the standard tier (<=1568px) — see ``core.domain.visual_budget``.
- Snapshot cost is approximated at ~chars/4 tokens (an English-text heuristic). The exact
  figure comes from Anthropic's ``count_tokens`` endpoint; the order-of-magnitude ratio is
  robust to the constant.

Run (from plugins/engram/):
    python3 bench/visual_tokens.py
    python3 bench/visual_tokens.py --url https://example.com   # needs a browser backend
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.domain.visual_budget import downscale_to_tier, estimate_visual_tokens  # noqa: E402

_CHARS_PER_TOKEN = 4  # rough English-text heuristic; count_tokens gives the exact value

# (name, viewport (w, h), a11y-tree snapshot text) — representative, deterministic fixtures.
FIXTURES: list[tuple[str, tuple[int, int], str]] = [
    (
        "login form",
        (1280, 800),
        '- heading "Sign in" [level=1]\n- textbox "Email"\n- textbox "Password"\n'
        '- button "Sign in"\n- link "Forgot password?"',
    ),
    (
        "article",
        (1440, 2400),
        '- banner:\n  - link "Home"\n  - navigation:\n    - link "Docs"\n    - link "Blog"\n'
        '- main:\n  - heading "Title" [level=1]\n  - paragraph: Lorem ipsum dolor sit amet\n'
        '  - heading "Section" [level=2]\n  - paragraph: More body text here\n- contentinfo: "© Example"',
    ),
    (
        "dashboard",
        (1920, 1080),
        '- navigation "Sidebar":\n  - link "Overview"\n  - link "Reports"\n  - link "Settings"\n'
        '- main:\n  - heading "Overview" [level=1]\n  - table "Metrics":\n'
        '    - row: "Revenue" "$12,340"\n    - row: "Users" "1,204"\n  - button "Export"',
    ),
]


def snapshot_tokens(text: str) -> int:
    """Approximate token cost of the a11y-tree text (~chars/4)."""
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))


def screenshot_tokens(width: int, height: int) -> int:
    """Visual-token cost of a screenshot at this viewport, capped to the standard tier."""
    return estimate_visual_tokens(*downscale_to_tier(width, height))


def compare(name: str, width: int, height: int, text: str) -> dict:
    ss = screenshot_tokens(width, height)
    sn = snapshot_tokens(text)
    return {
        "name": name,
        "viewport": f"{width}x{height}",
        "screenshot_tokens": ss,
        "a11y_chars": len(text),
        "a11y_tokens": sn,
        "ratio": ss / sn,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="a11y snapshot vs screenshot token cost")
    ap.add_argument("--url", help="snapshot a live page via the configured backend (needs playwright)")
    args = ap.parse_args()

    rows = [compare(name, w, h, text) for name, (w, h), text in FIXTURES]

    if args.url:
        from core.config import get_config
        from core.ports.snapshot import get_snapshotter

        cfg = get_config()
        view = get_snapshotter(cfg).snapshot(args.url)
        if view.is_empty:
            print(
                f"(live snapshot of {args.url} was empty — backend={cfg.snapshotter}; skipping the live row)",
                file=sys.stderr,
            )
        else:
            rows.append(compare(f"LIVE {args.url[:38]}", view.width or 1280, view.height or 800, view.text))

    print(f"{'page':<30}{'viewport':<12}{'screenshot tok':>15}{'a11y chars':>12}{'a11y ~tok':>11}{'ratio':>9}")
    print("-" * 89)
    for r in rows:
        print(
            f"{r['name']:<30}{r['viewport']:<12}{r['screenshot_tokens']:>15}"
            f"{r['a11y_chars']:>12}{r['a11y_tokens']:>11}{r['ratio']:>8.1f}x"
        )
    ratios = sorted(r["ratio"] for r in rows)
    print("-" * 89)
    print(f"median ratio: {ratios[len(ratios) // 2]:.1f}x  (a11y snapshot vs screenshot; higher = cheaper)")
    print(
        "note: screenshot = ceil(w/28)*ceil(h/28) capped to <=1568px; a11y ~tok = chars/4 (approx). "
        "Not engram eval — a standalone visual-token measure."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
