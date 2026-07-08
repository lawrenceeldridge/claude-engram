#!/usr/bin/env python3
"""UserPromptSubmit hook — just-in-time, query-specific recall.

Embeds the current prompt, retrieves the top few relevant facts for this project,
and injects them (threshold-gated, byte-capped) via ``additionalContext``. Fails
open: any error or empty result prints nothing and exits 0, so memory can never
block or break a turn. Lands at the tail of the message array (not the cached
prefix), so keep it small.
"""

from __future__ import annotations

import json
import os
import sys

from _bootstrap import plugin_root, reexec_if_pinned

reexec_if_pinned()
plugin_root()


def main() -> int:
    from _bootstrap import hooks_disabled

    if hooks_disabled():
        return 0  # inside an engram-spawned `claude -p` — stay inert
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return 0
    cwd = payload.get("cwd") or os.getcwd()

    try:
        from core.config import get_config
        from core.daemon_client import request
        from core.ports.embedding import get_embedder
        from core.project import resolve_project
        from core.service import recall_prompt_block
        from core.store import Store

        cfg = get_config()
        project = resolve_project(cwd, cfg.markers, identity=cfg.identity, project_dir=cfg.project_dir)

        # Prefer the warm daemon (avoids per-prompt model load); fall back in-process.
        # Pass the already-resolved project so the long-lived daemon never re-resolves
        # with its own (wrong-session) CLAUDE_PROJECT_DIR.
        block = None
        resp = request(cfg.sock_path, {"op": "recall", "project": dict(project), "cwd": cwd, "prompt": prompt})
        if resp is not None and "block" in resp:
            block = resp["block"]

        if block is None:
            store = Store(cfg.db_path)
            block = recall_prompt_block(store, get_embedder(cfg), cfg, project, prompt)
            store.close()

        if block:
            print(json.dumps({"additionalContext": block}))
    except Exception as exc:  # fail-open backstop
        print(f"[engram] recall skipped: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
