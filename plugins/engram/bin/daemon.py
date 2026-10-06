#!/usr/bin/env python3
"""Optional resident daemon — keeps the embedder and DB connection warm.

Short-lived hook processes would otherwise reload the embedding model on every
turn (seconds, with a real ONNX model). The daemon holds it warm and answers
recall over a Unix socket. Single-threaded on purpose: one recall takes ~10 ms on a
personal project (~0.5 s at 10⁵ facts) and a serial loop sidesteps SQLite's per-thread
connection rule.

SessionStart starts it when fastembed is provisioned (``engram daemon`` runs one by hand); the
recall hooks use it when it answers and silently fall back to in-process recall when not.
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


def serve() -> None:
    from core import singleflight
    from core.config import get_config

    cfg = get_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    # ensure_daemon pings-then-spawns, but a daemon takes seconds to boot (venv re-exec + model
    # load); several concurrent SessionStarts would each spawn one, and serve() unlinks-and-rebinds
    # the socket so none fail on bind — leaving orphans that each pin a warm model in RAM. Taking
    # the lock BEFORE loading the model means the racers exit cheaply and exactly one survives.
    with singleflight.held(Path(cfg.data_dir) / ".daemon.lock") as mine:
        if mine:
            _serve(cfg)


def _project_from_req(req: dict, cfg, resolve_project):
    """Resolve the request's project on the daemon side.

    The daemon is long-lived and serves many sessions, so it must never re-resolve using
    its OWN ``CLAUDE_PROJECT_DIR`` (that of whichever session happened to start it). It
    prefers the caller's already-resolved ``project`` and otherwise resolves from the
    request's ``cwd`` + ``project_dir`` (both carried per-request), only the mode
    (``identity``) coming from the daemon's config.
    """
    pre = req.get("project")
    if pre:
        return pre
    return resolve_project(req.get("cwd"), cfg.markers, identity=cfg.identity, project_dir=req.get("project_dir"))


def _serve(cfg) -> None:
    from core import errlog
    from core.ports.embedding import get_embedder
    from core.project import resolve_project
    from core.service import recall_core_block, recall_prompt_block, recall_structured
    from core.store import INTERACTIVE_BUSY_MS, Store

    sock_path = str(cfg.sock_path)
    try:
        os.unlink(sock_path)
    except OSError:
        pass

    # It serves only the interactive recall hooks: their ledger writes fail fast, never stall a prompt.
    store = Store(cfg.db_path, busy_timeout_ms=INTERACTIVE_BUSY_MS)
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
            op = None  # named in the stray-transaction record, whatever the request was
            try:
                req = json.loads(line)
                op = req.get("op")
                if op == "ping":
                    resp = {"ok": True}
                elif op == "recall":
                    project = _project_from_req(req, cfg, resolve_project)
                    resp = {"block": recall_prompt_block(store, embedder, cfg, project, req.get("prompt", ""))}
                elif op == "core":
                    project = _project_from_req(req, cfg, resolve_project)
                    resp = {"block": recall_core_block(store, cfg, project)}
                elif op == "recall_structured":
                    # MCP delegates here so recall shares the daemon's warm embedder — no write/read space drift.
                    project = _project_from_req(req, cfg, resolve_project)
                    resp = recall_structured(store, embedder, cfg, project, req.get("query", ""), k=req.get("k"))
                else:
                    resp = {"error": f"unknown op {op!r}"}
            except Exception as exc:
                resp = {"error": str(exc)}
            if store.end_stray_transaction():
                errlog.record(cfg.data_dir, "daemon", f"op {op!r} left a write transaction open — rolled back")
            conn.sendall((json.dumps(resp) + "\n").encode())


if __name__ == "__main__":
    try:
        serve()
    except KeyboardInterrupt:
        sys.exit(0)
