"""Project identity via marker-walk.

claude-mem keys memory on ``basename(cwd)``, which fragments monorepos and
subdirectory launches and collides across same-named folders. Instead we walk up
from the working directory to the nearest project marker (``.git`` etc.) and use
that directory's absolute path as a stable key (hashed for storage), with its
basename as a human label. This is stable regardless of which subdirectory the
session was launched from, and configurable for monorepo granularity.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TypedDict


class Project(TypedDict):
    key: str
    path: str
    label: str


def resolve_project(cwd: str | None, markers: tuple[str, ...]) -> Project:
    start = Path(cwd).resolve() if cwd else Path.cwd()
    root: Path | None = None
    for parent in (start, *start.parents):
        if any((parent / marker).exists() for marker in markers):
            root = parent
            break
    if root is None:
        root = start
    label = root.name or "root"
    key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return {"key": key, "path": str(root), "label": label}
