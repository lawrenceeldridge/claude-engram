"""Read a live engram store safely: bench tools work on a snapshot, never the original."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from core.project import Project
from core.store import Store


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


def find_project(store: Store, ref: str) -> Project | None:
    """The project whose key or label equals ``ref`` exactly."""
    for row in store.projects():
        if ref in (row["project_key"], row["project_label"]):
            return store.project_meta(row["project_key"])
    return None


def store_source(args: argparse.Namespace, cfg, tag: str) -> Path | None:
    """The real store a bench mode reads (``--store-db``, else the configured one), or ``None``
    with a message when ``--store-project`` is missing or there is no DB there."""
    if not args.store_project:
        print(f"[{tag}] needs --store-project (a project key or label)")
        return None
    source = Path(args.store_db or cfg.db_path)
    if not source.is_file():
        print(f"[{tag}] no engram DB at {source} (pass --store-db)")
        return None
    return source


@contextmanager
def snapshot_project(source: Path, ref: str) -> Iterator[tuple[Store, Project]]:
    """A fresh snapshot of ``source`` opened as a ``Store``, with the project ``ref`` in it.

    Raises ``LookupError`` when no project matches. Writes (e.g. a consolidation pass) land on
    the snapshot, which is deleted on exit; the source is only ever opened read-only.
    """
    with snapshot_db(source) as snapshot:
        store = Store(snapshot)
        try:
            project = find_project(store, ref)
            if project is None:
                raise LookupError(f"no project {ref!r} in {source}")
            yield store, project
        finally:
            store.close()
