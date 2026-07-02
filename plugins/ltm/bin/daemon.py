#!/usr/bin/env python3
"""Optional resident daemon — keeps the embedder and DB connection warm.

Short-lived hook processes would otherwise reload the embedding model on every
turn (seconds, with a real ONNX model). The daemon holds it warm and answers
recall over a Unix socket. Single-threaded on purpose: recall is sub-10ms and a
serial loop sidesteps SQLite's per-thread connection rule.

Run manually (``ltm daemon``) and set ``LTM_DAEMON=1`` so the recall hook uses it;
if it is not running, the hook silently falls back to in-process recall.
"""

from __future__ import annotations

import json
import os
import socket
import sys

from _bootstrap import plugin_root, reexec_if_pinned

reexec_if_pinned()
plugin_root()


def serve() -> None:
    from core.config import get_config
    from core.embedding import get_embedder
    from core.project import resolve_project
    from core.service import recall_core_block, recall_prompt_block
    from core.store import Store

    cfg = get_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
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
    print(f"[ltm] daemon listening on {sock_path} (embedding={cfg.embedding})")

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
