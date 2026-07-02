#!/usr/bin/env python3
"""SessionStart hook — refresh the project's documentation index in the background.

Indexing embeds section text, so like capture it spawns a detached worker and returns
immediately: zero interactive-token cost, no latency on session start. The refresh is
incremental (unchanged files short-circuit on a content hash), so the steady-state
cost after the first index is a directory walk plus a stat per file. Fails open.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from _bootstrap import plugin_root, reexec_if_pinned

reexec_if_pinned()
plugin_root()


def _run_worker(payload_path: str) -> None:
    try:
        with open(payload_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    finally:
        try:
            os.unlink(payload_path)
        except OSError:
            pass

    from core.config import get_config
    from core.embedding import get_embedder
    from core.indexer import index_project
    from core.project import resolve_project
    from core.store import Store

    cfg = get_config()
    root = payload.get("cwd") or os.getcwd()
    project = resolve_project(root, cfg.markers)
    embedder = get_embedder(cfg)
    store = Store(cfg.db_path)
    index_project(store, embedder, cfg, project, project["path"] or root)
    store.close()


def main() -> int:
    if "--worker" in sys.argv:
        _run_worker(sys.argv[-1])
        return 0

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    try:
        fd, payload_path = tempfile.mkstemp(prefix="ltm-idx-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", payload_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:  # fail-open backstop
        print(f"[ltm] index spawn failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
