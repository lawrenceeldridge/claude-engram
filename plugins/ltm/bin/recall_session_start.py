#!/usr/bin/env python3
"""SessionStart hook — inject a small, stable "project memory core".

This fires once and its text sits near the head of the message array, so it joins
the cached prefix and is read at cache rates on every subsequent turn — the
cache-friendly counterpart to the per-prompt hook. Deterministic ordering keeps
it stable within a session. Fails open.
"""

from __future__ import annotations

import json
import os
import sys

from _bootstrap import plugin_root

plugin_root()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    cwd = payload.get("cwd") or os.getcwd()

    try:
        from core.config import get_config
        from core.project import resolve_project
        from core.service import recall_core_block
        from core.store import Store

        cfg = get_config()
        if cfg.core_size <= 0:
            return 0
        project = resolve_project(cwd, cfg.markers)
        store = Store(cfg.db_path)
        block = recall_core_block(store, cfg, project)
        store.close()
        if block:
            print(json.dumps({"additionalContext": block}))
    except Exception as exc:  # fail-open backstop
        print(f"[ltm] core recall skipped: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
