#!/usr/bin/env python3
"""PreToolUse nudge — steer page reads toward the cheap accessibility-tree snapshot.

When the model is about to take a page SCREENSHOT (Chrome DevTools MCP
``take_screenshot``, Playwright MCP ``browser_take_screenshot``, BrowserMCP
``browser_screenshot``, …), a screenshot costs ~1,500+ vision tokens, whereas the
accessibility-tree TEXT snapshot (``take_snapshot`` / ``browser_snapshot``, or
engram's ``compact_page_view``) is ~10–50x cheaper and yields stable element
refs — enough for most visual/E2E structure and assertion work.

This hook can only *steer before* the call: it cannot reclaim tokens from an image
another server has already returned. Strength is set by ``ENGRAM_PREFER_SNAPSHOT``
(default ``advisory``): ``off`` disables it; ``advisory`` injects a once-per-session
reminder and always allows; ``strict`` denies the screenshot with the steer (opt-in —
use advisory if you also need genuine pixel checks). Fail-open: any error → exit 0,
inject nothing. Pure stdlib.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

# Any tool whose name looks like a page screenshot — deliberately broad so it catches
# Chrome DevTools MCP (take_screenshot), Playwright MCP (browser_take_screenshot) and
# BrowserMCP (browser_screenshot) whatever the server alias. engram's own tools have no
# screenshot, so this never self-fires. (The hooks.json matcher gates first; this is a
# defensive re-check.)
_SCREENSHOT_TOOL = re.compile(r"screenshot", re.I)

_NUDGE = (
    "claude-engram: a page SCREENSHOT costs ~1,500+ vision tokens. For structure, text and "
    "controls — what most visual/E2E assertions need — prefer the accessibility-tree TEXT "
    "snapshot: `take_snapshot` (Chrome DevTools MCP) or `browser_snapshot` (Playwright MCP), or "
    "engram's `compact_page_view`. It is ~10–50x cheaper and gives stable element refs. Take a "
    "screenshot only for a genuine pixel check (canvas/SVG/WebGL, visual regression)."
)
_STRICT_SUFFIX = (
    " [ENGRAM_PREFER_SNAPSHOT=strict is blocking this screenshot — set it to `advisory` to allow "
    "screenshots with a reminder, or `off` to silence.]"
)


def _emit_context(msg: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": msg}}))


def _emit_deny(msg: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": msg,
                }
            }
        )
    )


def _once(session: str, tag: str) -> bool:
    """True the first time (session, tag) is seen — dedupes a per-session nudge."""
    marker = Path(tempfile.gettempdir()) / f"engram-{tag}-{session}.seen"
    try:
        os.close(os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def main() -> int:
    from _bootstrap import hooks_disabled

    if hooks_disabled():
        return 0  # inside an engram-spawned `claude -p` — stay inert
    mode = os.environ.get("ENGRAM_PREFER_SNAPSHOT", "advisory").lower()
    if mode == "off":
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool = payload.get("tool_name", "")
    if not _SCREENSHOT_TOOL.search(tool):
        return 0  # the matcher is broad — confirm it really is a screenshot tool
    session = payload.get("session_id") or str(os.getppid())

    if mode == "strict":
        _emit_deny(_NUDGE + _STRICT_SUFFIX)
        return 0
    if _once(session, "prefer-snapshot"):  # advisory reminder, once per session
        _emit_context(_NUDGE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
