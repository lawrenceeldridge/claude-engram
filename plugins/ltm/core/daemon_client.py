"""Thin client for the optional resident daemon.

Recall hooks call ``request`` first; on any failure they fall back to running the
core in-process, so the daemon is a pure speed optimisation and can never break a
turn. The daemon matters most with the fastembed adapter, where it keeps the
model warm across the short-lived hook processes.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path


def request(sock_path: Path | str, payload: dict, timeout: float = 2.0) -> dict | None:
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(sock_path))
        sock.sendall((json.dumps(payload) + "\n").encode())
        with sock.makefile("r") as fh:
            line = fh.readline()
        sock.close()
        return json.loads(line) if line else None
    except (OSError, ValueError):
        return None


def ensure_daemon(sock_path: Path | str, plugin_root: str) -> None:
    """Start the resident daemon if it isn't already answering. No-op if up."""
    import os
    import subprocess
    import sys

    if request(sock_path, {"op": "ping"}, timeout=1) is not None:
        return
    daemon = os.path.join(plugin_root, "bin", "daemon.py")

    # Pin the daemon to the managed venv, not the caller's interpreter: a spawner
    # re-exec'd into a transient venv would otherwise leave the daemon running under
    # a soon-deleted python. Drop LTM_REEXECED so the daemon re-pins from scratch.
    interpreter = sys.executable
    try:
        from core.config import get_config
        from core.provision import venv_python

        managed = venv_python(get_config().data_dir)
        if os.path.exists(managed):
            interpreter = str(managed)
    except Exception:
        pass
    env = {key: value for key, value in os.environ.items() if key != "LTM_REEXECED"}
    try:
        subprocess.Popen(
            [interpreter, daemon],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except OSError:
        pass
