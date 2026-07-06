"""Invalidate — retire anti-patterns whose world has changed.

Anti-patterns are exempt from dormancy forgetting (refine / TTL sweep), so they need a
*different* obsolescence signal. A project-scoped rule that references files which no longer
exist on disk is stale — the thing it warned about is gone. Such a rule is archived reversibly
(``status='expired'`` — recall scans 'active' only; purge-eligible after the horizon), never
hard-deleted here.

Conservative by design: fires only when EVERY referenced file is gone, so a rule survives a
moved example. Global anti-patterns (no project path, no files) are never touched. This is the
local, LLM-free obsolescence signal (design Q4b-B); an LLM applicability judgement is deferred.
"""

from __future__ import annotations

import json
import os


def invalidate_stale_antipatterns(store, project, now: float | None = None) -> int:
    """Archive project anti-patterns whose referenced files have all vanished. Returns the count."""
    root = project.get("path") or ""
    if not root:
        return 0  # global / pathless project — no filesystem drift signal
    stale: list[str] = []
    for row in store.active_antipatterns(project["key"]):
        raw = row["files"]
        if not raw:
            continue  # a rule with no file anchor can't be drift-invalidated
        try:
            files = [f for f in json.loads(raw) if isinstance(f, str) and f.strip()]
        except (ValueError, TypeError):
            continue
        if files and all(not os.path.exists(os.path.join(root, f)) for f in files):
            stale.append(row["id"])
    return store.set_status(stale, "expired")
