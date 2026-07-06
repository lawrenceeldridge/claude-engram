#!/usr/bin/env python3
"""PostToolUse (Edit|Write|MultiEdit) — refresh the code/docs index for the changed file.

Keeps the index fresh mid-session instead of only at SessionStart. The edited path is
appended to a per-data-dir dirty list; a single detached worker (single-flight lock)
drains that list until empty, so a *burst* of edits coalesces into ONE process with ONE
embedder load rather than one worker per edit. Non-blocking and fail-open, and it never
touches the recall daemon, so recall latency is unaffected. The per-file update is
hash-short-circuited, so re-indexing an unchanged file is nearly free.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from _bootstrap import plugin_root, reexec_if_pinned

# Index-eligible extensions — kept in step with core.index.indexer's _INDEX_EXTENSIONS.
_EXT = {".md", ".markdown", ".mdx", ".mdc", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}


def _paths(data_dir) -> tuple[Path, Path]:
    return Path(data_dir) / ".index-dirty", Path(data_dir) / ".index-edit.lock"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire(lock: Path) -> bool:
    """Single-flight: one drain worker at a time (a dead holder is stolen)."""
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            holder = int(lock.read_text().strip() or 0)
        except (OSError, ValueError):
            holder = 0
        if holder and _alive(holder):
            return False
        try:
            lock.unlink()
        except OSError:
            return False
        return _acquire(lock)


def _run_worker() -> None:
    from core.config import get_config
    from core.index.indexer import index_file
    from core.ports.embedding import get_embedder
    from core.project import resolve_project
    from core.store import Store

    cfg = get_config()
    dirty, lock = _paths(cfg.data_dir)
    if not _acquire(lock):
        return  # another worker is draining; our appended entries are in its queue

    store = embedder = None
    try:
        proc = Path(str(dirty) + ".proc")
        while True:
            try:
                os.replace(dirty, proc)  # atomically claim the batch; new edits append to a fresh list
            except OSError:
                break  # nothing pending
            files, seen = [], set()
            for line in proc.read_text(encoding="utf-8", errors="ignore").splitlines():
                p = line.strip()
                if p and p not in seen:
                    seen.add(p)
                    files.append(p)
            proc.unlink(missing_ok=True)
            if not files:
                continue
            if store is None:
                store, embedder = Store(cfg.db_path), get_embedder(cfg)  # loaded once per burst
            for fp in files:
                try:
                    project = resolve_project(str(Path(fp).parent), cfg.markers)
                    index_file(store, embedder, cfg, project, fp)
                except Exception:
                    pass  # fail-open per file — one bad edit can't stall the rest
    finally:
        if store is not None:
            store.close()
        try:
            lock.unlink()
        except OSError:
            pass


def main() -> int:
    from _bootstrap import hooks_disabled

    if hooks_disabled():
        return 0  # inside an ltm-spawned `claude -p` — stay inert
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path or Path(file_path).suffix.lower() not in _EXT:
        return 0

    from core.config import get_config

    dirty, _lock = _paths(get_config().data_dir)
    try:
        with open(dirty, "a", encoding="utf-8") as fh:
            fh.write(str(file_path) + "\n")
    except OSError:
        return 0
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:  # fail-open backstop
        print(f"[ltm] index-edit spawn failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if "--worker" in sys.argv:
        reexec_if_pinned()  # into the managed venv (fastembed + tree-sitter) for the worker only
        plugin_root()
        sys.exit(_run_worker())
    plugin_root()  # main path stays on the ambient interpreter — just appends + spawns
    sys.exit(main())
