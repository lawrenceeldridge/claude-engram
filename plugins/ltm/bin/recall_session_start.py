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

from _bootstrap import plugin_root, reexec_if_pinned

reexec_if_pinned()
ROOT = plugin_root()

MEMORY_FIRST_POLICY = (
    "[ltm] Memory/index-first — at the start of a task, and before ANY codebase search or "
    "whole-file read, consult claude-ltm first (measured ~2/3 fewer tokens than grep+read on "
    "lookups). This applies to Grep/Glob/Task AND to grep/rg/find via Bash — those are searches "
    "too. Order: `recall` for prior decisions/facts; `search_code` / `search_docs` for this "
    "project's indexed symbols / doc sections (ranked outlines, not file scans); then "
    "`get_symbol` / `get_doc_section` for the exact span. Trust confident hits and skip the wider "
    "search; widen to Grep/Glob/Bash only when recall/search comes back weak or empty."
)


def main() -> int:
    from _bootstrap import hooks_disabled

    if hooks_disabled():
        return 0  # inside an ltm-spawned `claude -p` — stay inert
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    cwd = payload.get("cwd") or os.getcwd()

    try:
        from core.config import get_config
        from core.project import resolve_project
        from core.service import orientation_block, recall_core_block
        from core.store import Store

        cfg = get_config()
        if cfg.viewer_autostart:
            # Detached, idempotent: survives this session and only ever runs once.
            from viewer.serve import ensure_viewer

            ensure_viewer(cfg.viewer_port, str(ROOT))
        if cfg.embedding != "hash":
            from core.provision import is_provisioned

            if is_provisioned(cfg.data_dir):
                # Warm the resident daemon once per session so per-prompt recall is fast.
                from core.daemon_client import ensure_daemon

                ensure_daemon(cfg.sock_path, str(ROOT))
            else:
                # First run: build the fastembed venv in the background (non-blocking).
                import subprocess

                subprocess.Popen(
                    [sys.executable, str(ROOT / "bin" / "ltm"), "setup"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
        project = resolve_project(cwd, cfg.markers)
        store = Store(cfg.db_path)
        orientation = orientation_block(store, project)
        block = recall_core_block(store, cfg, project) if cfg.core_size > 0 else ""
        store.close()
        parts = [MEMORY_FIRST_POLICY]
        if orientation:
            parts.append(orientation)
        if block:
            parts.append(block)
        print(json.dumps({"additionalContext": "\n\n".join(parts)}))
    except Exception as exc:  # fail-open backstop
        print(f"[ltm] core recall skipped: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
