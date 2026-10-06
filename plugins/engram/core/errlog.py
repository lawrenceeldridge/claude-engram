"""Fail open, but leave a record — a small rotating error log in the data dir.

Every hook and worker swallows its errors so a turn never breaks; before this nothing kept them,
so a store write-locked for 13 h, or a 13.5 h consolidation, left no trace. ``record`` appends
one JSON line per event (``O_APPEND``: concurrent writers interleave whole lines); past
:data:`MAX_BYTES` the log rotates to one ``.1`` copy, so it can never grow without bound. It never
raises — a log that could break the caller would defeat its purpose. Read by ``core.health``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

LOG_NAME = "errors.log"
MAX_BYTES = 256 * 1024


def log_path(data_dir: str | os.PathLike) -> Path:
    return Path(data_dir) / LOG_NAME


def record(data_dir: str | os.PathLike, source: str, message: str, *, now: float | None = None) -> None:
    """Append ``{ts, source, message}`` to the data dir's error log; rotate past ``MAX_BYTES``."""
    try:
        path = log_path(data_dir)
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            os.replace(path, path.with_name(LOG_NAME + ".1"))
        line = json.dumps({"ts": time.time() if now is None else now, "source": source, "message": message})
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


def last(data_dir: str | os.PathLike) -> dict | None:
    """The newest event in the log, or ``None`` (no log, unreadable, or empty)."""
    try:
        with open(log_path(data_dir), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 4096))
            tail = fh.read().decode("utf-8", "replace").splitlines()
        for line in reversed(tail):
            try:
                return json.loads(line)
            except ValueError:
                continue
    except OSError:
        pass
    return None
