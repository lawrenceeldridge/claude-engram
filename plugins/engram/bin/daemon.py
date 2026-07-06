#!/usr/bin/env python3
"""Optional resident daemon — keeps the embedder and DB connection warm.

Short-lived hook processes would otherwise reload the embedding model on every
turn (seconds, with a real ONNX model). The daemon holds it warm and answers
recall over a Unix socket. Single-threaded on purpose: recall is sub-10ms and a
serial loop sidesteps SQLite's per-thread connection rule.

Run manually (``engram daemon``) and set ``ENGRAM_DAEMON=1`` so the recall hook uses it;
if it is not running, the hook silently falls back to in-process recall.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

from _bootstrap import plugin_root, reexec_if_pinned

reexec_if_pinned()
plugin_root()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_lock(lock: Path) -> bool:
    """Single-flight: only one daemon runs at a time (a dead holder is stolen).

    ensure_daemon pings-then-spawns, but the daemon takes seconds to boot (venv re-exec
    + embedding-model load); several concurrent SessionStarts would each spawn one, and
    serve() unlinks-and-rebinds the socket so none fail on bind — leaving orphaned daemons
    that each pin a warm model in RAM forever. Grabbing this lock BEFORE loading the model
    means the racers exit cheaply and exactly one daemon survives.
    """
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
        return _acquire_lock(lock)


def serve() -> None:
    from core.config import get_config

    cfg = get_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    lock = Path(cfg.data_dir) / ".daemon.lock"
    if not _acquire_lock(lock):
        return  # another live daemon already owns the socket + warm model
    try:
        _serve(cfg, lock)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _serve(cfg, lock: Path) -> None:
    from core.ports.embedding import get_embedder
    from core.project import resolve_project
    from core.service import recall_core_block, recall_prompt_block, recall_structured
    from core.store import Store

    sock_path = str(cfg.sock_path)
    try:
        os.unlink(sock_path)
    except OSError:
        pass

    store = Store(cfg.db_path)
    embedder = get_embedder(cfg)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(16)
    print(f"[engram] daemon listening on {sock_path} (embedding={cfg.embedding})")

    while True:
        conn, _ = server.accept()
        with conn, conn.makefile("r") as reader:
            line = reader.readline()
            if not line:
                continue
            try:
                req = json.loads(line)
                op = req.get("op")
                if op == "ping":
                    resp = {"ok": True}
                elif op == "recall":
                    project = resolve_project(req.get("cwd"), cfg.markers)
                    resp = {"block": recall_prompt_block(store, embedder, cfg, project, req.get("prompt", ""))}
                elif op == "core":
                    project = resolve_project(req.get("cwd"), cfg.markers)
                    resp = {"block": recall_core_block(store, cfg, project)}
                elif op == "recall_structured":
                    # MCP delegates here so recall shares the daemon's warm embedder — no write/read space drift.
                    project = req.get("project") or resolve_project(req.get("cwd"), cfg.markers)
                    resp = recall_structured(store, embedder, cfg, project, req.get("query", ""), k=req.get("k"))
                else:
                    resp = {"error": f"unknown op {op!r}"}
            except Exception as exc:
                resp = {"error": str(exc)}
            conn.sendall((json.dumps(resp) + "\n").encode())


if __name__ == "__main__":
    try:
        serve()
    except KeyboardInterrupt:
        sys.exit(0)
