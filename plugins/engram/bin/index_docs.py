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

# Unattended refresh caps eligible files: resolving to a monorepo git-root can surface
# tens of thousands of files, and embedding those on session start is never wanted. A
# tree over the cap is skipped whole — index the subtree you care about explicitly via
# the index_docs tool (which is unbounded). Keeps auto-index safe on huge repos.
_AUTO_MAX_FILES = 4000


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_lock(path) -> bool:
    """Single-flight lock: only one index worker runs at a time (a dead holder is stolen)."""
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            holder = int(path.read_text().strip() or 0)
        except (OSError, ValueError):
            holder = 0
        if holder and _alive(holder):
            return False  # another worker is already indexing — don't pile on
        try:
            path.unlink()
        except OSError:
            return False
        return _acquire_lock(path)


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
    from core.index.indexer import index_project, tree_signature
    from core.ports.embedding import get_embedder
    from core.project import resolve_project
    from core.store import Store

    cfg = get_config()
    lock = Path(cfg.data_dir) / ".index.lock"
    if not _acquire_lock(lock):
        return
    try:
        root = payload.get("cwd") or os.getcwd()
        project = resolve_project(root, cfg.markers, identity=cfg.identity, project_dir=cfg.project_dir)
        index_root = project["path"] or root
        store = Store(cfg.db_path)
        # Merkle rollup: if the file tree is unchanged since the last index, skip the
        # whole pass — including loading the embedding model (the expensive part).
        sig_key = f"idxsig:{project['key']}"
        sig = tree_signature(index_root)
        if sig != 0 and sig == store.get_capture_cursor(sig_key):
            store.close()
            return
        embedder = get_embedder(cfg)
        index_project(store, embedder, cfg, project, index_root, max_files=_AUTO_MAX_FILES)
        store.set_capture_cursor(sig_key, sig)
        store.close()
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def main() -> int:
    from _bootstrap import hooks_disabled

    if hooks_disabled():
        return 0  # inside an engram-spawned `claude -p` — stay inert
    if "--worker" in sys.argv:
        _run_worker(sys.argv[-1])
        return 0

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    try:
        fd, payload_path = tempfile.mkstemp(prefix="engram-idx-", suffix=".json")
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
        print(f"[engram] index spawn failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
