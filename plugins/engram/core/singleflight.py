"""Single-flight: at most one process runs a job at a time, by a pid lock file (a dead holder's
lock is reclaimed).

The capture worker, the consolidation pass, the indexer, the edit-reindex drain and the daemon
each hold one, so a burst of hook events can't pile up processes that each load the embedder.
Every job is cursor- or queue-based, so a skipped run's work is picked up by the next one —
serialising loses nothing. ``holder`` lets ``core.health`` say who holds a lock and for how long.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _read_pid(lock: Path) -> int:
    try:
        return int(lock.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def acquire(lock: Path) -> bool:
    """Take ``lock`` for this process, reclaiming it from a dead holder; ``False`` if a live one has it."""
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = _read_pid(lock)
        if holder and _alive(holder):
            return False
        try:
            lock.unlink()
        except OSError:
            return False
        return acquire(lock)
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    return True


def release(lock: Path) -> None:
    """Drop ``lock`` if this process holds it (never another's)."""
    if _read_pid(lock) == os.getpid():
        try:
            lock.unlink()
        except OSError:
            pass


@contextmanager
def held(lock: Path) -> Iterator[bool]:
    """``with held(lock) as mine:`` — run the block holding ``lock`` when ``mine`` (released on exit);
    ``mine`` is ``False`` when a live process already holds it."""
    mine = acquire(lock)
    try:
        yield mine
    finally:
        if mine:
            release(lock)


def holder(lock: Path, now: float | None = None) -> tuple[int, float] | None:
    """``(pid, seconds held)`` of the live process holding ``lock``, or ``None`` (free, or a dead holder's)."""
    pid = _read_pid(lock)
    if not pid or not _alive(pid):
        return None
    try:
        since = lock.stat().st_mtime
    except OSError:
        return None
    return pid, (time.time() if now is None else now) - since
