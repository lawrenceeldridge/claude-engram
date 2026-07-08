"""Project identity — keyed on the workspace root, hashed for a collision-free key.

The project a memory belongs to is, by default, **the folder the session was opened
in** (``identity='workspace'``): the directory Claude Code was started in
(``CLAUDE_PROJECT_DIR``), or the working directory when that is absent. That folder's
absolute path is hashed into a stable key and its basename becomes the human label.
This matches the human's chosen boundary — a monorepo subfolder opened as a workspace
(``…/ips-applications/applications/dune/moj-sak``) stays its own ``moj-sak`` project
rather than being folded into the monorepo root, and a repo opened at its top
(``…/claude-engram``) does not fragment into a nested package (``plugins/engram``).

Hashing the path (not using ``basename(cwd)`` as the key) keeps it collision-free: two
different ``backend`` folders get distinct keys though they share a label.

Two escape hatches:

- ``identity='marker'`` restores the legacy behaviour — walk up from the working
  directory to the nearest project marker (``.git`` etc.) and key on that. Useful when
  sessions are launched from deep subdirectories and should consolidate upward.
- An explicit ``.engram-root`` sentinel file **overrides both modes**: drop one in a
  directory to pin it as the project root (the nearest ``.engram-root`` ancestor wins).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TypedDict

ROOT_MARKER = ".engram-root"


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


def resolve_project(
    cwd: str | None,
    markers: tuple[str, ...],
    *,
    identity: str = "workspace",
    project_dir: str | None = None,
) -> Project:
    """Resolve the project a session/file belongs to.

    ``identity='workspace'`` (default) anchors on ``project_dir`` (the folder Claude was
    started in) when given, else ``cwd``, and uses that folder directly — no walk.
    ``identity='marker'`` walks up from ``cwd`` to the nearest ancestor holding a project
    marker. A ``.engram-root`` sentinel in any ancestor overrides both. Pure function
    (only touches the filesystem to test for marker existence); env/config reads happen
    in the composition roots that call it.
    """
    # In workspace mode the anchor is the dir Claude was started in (stable across a
    # terminal `cd`); in marker mode we always start from cwd and walk up to find the root.
    anchor = project_dir if (identity == "workspace" and project_dir) else cwd
    start = Path(anchor).resolve() if anchor else Path.cwd()
    ancestors = (start, *start.parents)
    # 1. An explicit .engram-root sentinel pins the project root (highest precedence, both modes).
    root = next((p for p in ancestors if (p / ROOT_MARKER).exists()), None)
    if root is None:
        if identity == "marker":
            # 2a. Legacy: the nearest ancestor holding any configured project marker.
            root = next((p for p in ancestors if any((p / m).exists() for m in markers)), None)
        else:
            # 2b. Workspace: the anchor folder is the project — used as-is, never walked.
            root = start
    if root is None:
        root = start
    label = root.name or "root"
    key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return {"key": key, "path": str(root), "label": label}
