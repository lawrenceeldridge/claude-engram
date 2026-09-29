"""Read a live engram store safely: bench tools work on a snapshot, never the original."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def snapshot_db(src: Path) -> Iterator[Path]:
    """Consistent copy of an engram DB (SQLite online backup, source opened read-only) in a
    temp dir that is removed on exit — a multi-GB store must not linger in /tmp."""
    root = Path(tempfile.mkdtemp(prefix="engram-snapshot-"))
    dest = root / "snapshot.db"
    try:
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        yield dest
    finally:
        shutil.rmtree(root, ignore_errors=True)
