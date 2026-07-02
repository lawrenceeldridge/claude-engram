#!/usr/bin/env python3
"""SessionEnd / PreCompact hook — capture the session into memory.

Distillation + embedding are heavy, so the hook spawns a detached worker and
returns immediately: zero interactive-token cost and no latency added to the
user's turn. The worker reads the payload from a temp file, distils the
transcript into atomic facts, embeds and persists them, then deletes the file.
Fails open.
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
    from core.project import resolve_project
    from core.service import capture_transcript_incremental
    from core.store import Store

    cfg = get_config()
    project = resolve_project(payload.get("cwd") or os.getcwd(), cfg.markers)
    transcript_path = payload.get("transcript_path")
    if not transcript_path or not Path(transcript_path).exists():
        return
    store = Store(cfg.db_path)
    capture_transcript_incremental(store, get_embedder(cfg), cfg, project, payload.get("session_id", ""), transcript_path)
    if cfg.ttl_days > 0:
        import time

        store.sweep(time.time(), cfg.ttl_days * 86400, cfg.ttl_keep_frequency, project["key"])
    store.close()


def main() -> int:
    if "--worker" in sys.argv:
        _run_worker(sys.argv[-1])
        return 0

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    try:
        fd, payload_path = tempfile.mkstemp(prefix="ltm-cap-", suffix=".json")
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
        print(f"[ltm] capture spawn failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
