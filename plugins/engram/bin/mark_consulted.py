#!/usr/bin/env python3
"""PostToolUse (engram memory/index tools) — mark that memory was consulted this session.

Drops a per-session marker the moment the model calls recall / search_code / search_docs
(or a follow-up get_/outline). The PreToolUse guard reads that marker to enforce order:
a broad Grep/Glob is held back until memory has been consulted, then flows freely. Pure
stdlib, instant, fail-open. Marks for any engram tool except index_docs (a maintenance write,
not a lookup).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

_NON_CONSULT = {"index_docs"}  # a write/refresh, not a memory lookup


def main() -> int:
    from _bootstrap import hooks_disabled

    if hooks_disabled():
        return 0  # inside an engram-spawned `claude -p` — stay inert
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool = (payload.get("tool_name") or "").rsplit("__", 1)[-1]  # bare tool name after the mcp prefix
    if tool in _NON_CONSULT:
        return 0
    session = payload.get("session_id") or str(os.getppid())
    try:
        open(os.path.join(tempfile.gettempdir(), f"engram-consulted-{session}.seen"), "w").close()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
