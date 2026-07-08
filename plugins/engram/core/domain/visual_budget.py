"""Pure visual-budget arithmetic for the snapshot-compaction tool.

Functional Core: no I/O, no image library, no clock — pure functions over scalar
dimensions, so they stay stdlib-testable without mocks and are reusable by both
the token micro-benchmark and the (deferred v2) pixel path.

The token formula mirrors how Claude bills images: an image is tokenised in
28x28-pixel patches, so it costs ``ceil(w / 28) * ceil(h / 28)`` visual tokens.
See DESIGN.md and the ``visual-token-reduction-research`` memory.
"""

from __future__ import annotations

import math

# Claude's visual-token patch size — 28x28-px blocks.
_PATCH = 28

# Standard-tier long-edge cap (px). High-resolution models allow more (2576 px) at
# up to ~3x the token cost; v1 targets the standard tier to keep any image cheap.
STANDARD_TIER_PX = 1568


def estimate_visual_tokens(width: int, height: int) -> int:
    """Visual-token cost of an image at ``width`` x ``height`` px.

    ``ceil(width / 28) * ceil(height / 28)`` — the patch-grid formula Claude uses
    (e.g. 200x200 -> 64, 1000x1000 -> 1296, 1092x1092 -> 1521). Returns 0 for a
    non-positive dimension (nothing to bill).
    """
    if width <= 0 or height <= 0:
        return 0
    return math.ceil(width / _PATCH) * math.ceil(height / _PATCH)


def downscale_to_tier(width: int, height: int, tier_px: int = STANDARD_TIER_PX) -> tuple[int, int]:
    """Aspect-preserving dimensions with the long edge capped at ``tier_px``.

    A no-op when the image already fits (never upscales). Keeping the long edge
    <= 1568 px stays on the cheaper standard tier — the primary token-control knob
    for the pixel path.
    """
    long_edge = max(width, height)
    if long_edge <= 0 or long_edge <= tier_px:
        return (width, height)
    scale = tier_px / long_edge
    return (max(1, round(width * scale)), max(1, round(height * scale)))
