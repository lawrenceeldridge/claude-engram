#!/usr/bin/env python3
"""PostToolUse (Read) — credit the whole-file read avoided by a bounded (offset/limit) Read.

The usage ledger books `get_symbol` / `get_doc_section` body-fetches as *measured* savings
(file bytes − returned body). A bounded `Read` of an indexed file is the same "targeted read
instead of the whole file" win, so credit it too — otherwise the ledger reads flat during
exactly the token-frugal edit-cycle work it's meant to reward.

Conservative and fail-open, consistent with the ledger being a floor:
  - only a *bounded* read (offset/limit present) — a whole-file Read saved nothing;
  - only files the project has **indexed** (mirrors the get_symbol credit; skips noise);
  - saving = file bytes − returned span; skipped if the span can't be measured;
  - any error → credit nothing, exit 0. Never touches recall latency.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from _bootstrap import plugin_root


def _returned_bytes(tool_response) -> int:
    """UTF-8 byte length of the longest string in the tool response — the returned span.

    Robust to the Read response shape (bare string, or nested under content/file/text):
    the file content is the longest string. 0 means "couldn't measure" → don't credit.
    """
    if isinstance(tool_response, str):
        return len(tool_response.encode("utf-8", "ignore"))
    if isinstance(tool_response, dict):
        return max((_returned_bytes(v) for v in tool_response.values()), default=0)
    if isinstance(tool_response, list):
        return max((_returned_bytes(v) for v in tool_response), default=0)
    return 0


def main() -> int:
    from _bootstrap import hooks_disabled

    if hooks_disabled():
        return 0  # inside an engram-spawned `claude -p` — stay inert
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path")
    # A *bounded* read only — a whole-file Read (no offset/limit) avoided nothing.
    if not file_path or ("offset" not in tool_input and "limit" not in tool_input):
        return 0
    try:
        from core.config import get_config
        from core.project import resolve_project
        from core.store import Store

        full = os.path.getsize(file_path)
        returned = _returned_bytes(payload.get("tool_response"))
        if returned <= 0 or full <= returned:
            return 0  # unmeasurable span, or no saving — stay conservative

        # Resolve symlinks (e.g. macOS /var → /private/var) so the relative path matches
        # how the indexer stored source_path — otherwise source_state never resolves.
        real = os.path.realpath(file_path)
        cfg = get_config()
        project = resolve_project(str(Path(real).parent), cfg.markers)
        rel = os.path.relpath(real, project["path"])
        store = Store(cfg.db_path)
        try:
            if store.source_state(project["key"], rel) is not None:  # indexed only
                store.record_usage(project["key"], "read_bounded", bytes_saved=full - returned)
        finally:
            store.close()
    except Exception:
        return 0  # fail-open — crediting is best-effort, never breaks a Read
    return 0


if __name__ == "__main__":
    plugin_root()
    sys.exit(main())
