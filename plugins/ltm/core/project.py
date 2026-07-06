"""Project identity via marker-walk.

Keying memory on ``basename(cwd)`` fragments monorepos and subdirectory launches
and collides across same-named folders. Instead we walk up
from the working directory to the nearest project marker (``.git`` etc.) and use
that directory's absolute path as a stable key (hashed for storage), with its
basename as a human label. This is stable regardless of which subdirectory the
session was launched from, and configurable for monorepo granularity.

An explicit ``.ltm-root`` sentinel file takes precedence over the marker walk: drop
one in a directory to pin it as the project root, so a repo whose subfolders each
carry their own ``pyproject.toml`` / ``package.json`` (a plugin package, an app's
``backend``/``frontend``) still resolves to a single project instead of fragmenting.
The nearest ``.ltm-root`` ancestor wins.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TypedDict

ROOT_MARKER = ".ltm-root"


class Project(TypedDict):
    key: str
    path: str
    label: str


# Reserved key for globally-scoped memory that applies across every project. Only
# anti-patterns (tool/harness lessons) use it today; a real project can never collide
# because project keys are 16-hex-char sha256 digests (see resolve_project).
GLOBAL_PROJECT_KEY = "__global__"


def global_project() -> Project:
    """The synthetic project that owns globally-scoped anti-patterns."""
    return {"key": GLOBAL_PROJECT_KEY, "path": "", "label": "global"}


def resolve_project(cwd: str | None, markers: tuple[str, ...]) -> Project:
    start = Path(cwd).resolve() if cwd else Path.cwd()
    ancestors = (start, *start.parents)
    # 1. An explicit .ltm-root sentinel pins the project root (highest precedence).
    root = next((p for p in ancestors if (p / ROOT_MARKER).exists()), None)
    # 2. Otherwise, the nearest ancestor holding any configured project marker.
    if root is None:
        root = next((p for p in ancestors if any((p / m).exists() for m in markers)), None)
    if root is None:
        root = start
    label = root.name or "root"
    key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return {"key": key, "path": str(root), "label": label}
