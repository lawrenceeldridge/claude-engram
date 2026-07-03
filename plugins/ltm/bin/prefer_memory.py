#!/usr/bin/env python3
"""PreToolUse guard — steer toward memory/index before a broad search or a raw code read.

Two behaviours, both fail-open:
  * Grep / Glob / Task — a once-per-session reminder to consult recall / search_code /
    search_docs before scanning.
  * Read of a large code file (>=4KB, no offset/limit) — suggest search_code + get_symbol
    instead of pulling the whole file.

Strength is set by ``LTM_ENFORCE`` (default ``advisory``): ``off`` disables the guard;
``advisory`` only injects reminders and never blocks; ``strict`` turns the large-code-Read
case into a real ``deny`` — but only for files actually in the index, and never for
offset/limit reads, so an agent can still do a targeted pre-edit read. Pure stdlib on the
common path; the strict "is it indexed?" check lazily opens the store only under ``strict``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_MIN_READ_BYTES = 4096

_SEARCH_REMINDER = (
    "claude-ltm memory + index are cheaper than a broad search (measured ~2/3 fewer tokens on "
    "lookups). Before Grep/Glob/Task, consult `recall` (prior facts), then `search_code` / "
    "`search_docs` (indexed symbols / doc sections), then `get_symbol` / `get_doc_section` for the "
    "exact span. Trust confident hits; widen only if weak or empty. (Fires once per session.)"
)
_READ_ADVICE = (
    "Reading a large code file whole — `search_code` then `get_symbol` returns just the symbol you "
    "need (measured ~2/3 fewer tokens). Read is fine when you're about to edit it. (Fires once per session.)"
)


def _once(session: str, tag: str) -> bool:
    """True the first time (session, tag) is seen — dedupes a per-session nudge."""
    marker = Path(tempfile.gettempdir()) / f"ltm-{tag}-{session}.seen"
    try:
        os.close(os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def _consulted(session: str) -> bool:
    """Whether recall/search_code/search_docs has been called this session (marker from mark_consulted.py)."""
    return (Path(tempfile.gettempdir()) / f"ltm-consulted-{session}.seen").exists()


def _emit_context(msg: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": msg}}))


def _emit_deny(msg: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": msg,
                }
            }
        )
    )


def _is_indexed(file_path: str) -> bool:
    """Whether this file has chunks in the index — gate strict denies to indexed code only."""
    try:
        from _bootstrap import plugin_root

        plugin_root()
        from core.config import get_config
        from core.project import resolve_project
        from core.store import Store

        cfg = get_config()
        path = Path(file_path).resolve()  # match index_file's resolved source paths (symlink-safe)
        project = resolve_project(str(path.parent), cfg.markers)
        root = Path(project["path"]).resolve() if project.get("path") else path.parent
        if not path.is_relative_to(root):
            return False
        store = Store(cfg.db_path)
        try:
            return store.source_state(project["key"], str(path.relative_to(root))) is not None
        finally:
            store.close()
    except Exception:
        return False


def main() -> int:
    enforce = os.environ.get("LTM_ENFORCE", "advisory").lower()
    if enforce == "off":
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    session = payload.get("session_id") or str(os.getppid())

    if tool == "Read":
        fp = tool_input.get("file_path", "")
        if Path(fp).suffix.lower() in _CODE_EXT and not tool_input.get("offset") and not tool_input.get("limit"):
            try:
                size = os.path.getsize(fp)
            except OSError:
                size = 0
            if size >= _MIN_READ_BYTES:
                if enforce == "strict" and _is_indexed(fp):
                    _emit_deny(
                        "This file is indexed by claude-ltm. Use `search_code` + `get_symbol` to read the "
                        "specific symbol (far fewer tokens), or Read with offset/limit for a pre-edit peek. "
                        "Set LTM_ENFORCE=advisory to disable this gate."
                    )
                elif _once(session, "readguard"):
                    _emit_context(_READ_ADVICE)
        return 0

    # Grep / Glob / Task — enforce cheap-check-first ordering.
    consulted = _consulted(session)
    if tool in ("Grep", "Glob") and not consulted and enforce == "strict":
        _emit_deny(
            "Consult claude-ltm first — call `recall` and `search_code` / `search_docs` before a broad "
            "search. Once you've checked memory/index, Grep/Glob flow freely (widen when they come back "
            "weak or empty). Set LTM_ENFORCE=advisory to make this a reminder instead of a gate."
        )
        return 0
    if not consulted and _once(session, "prefer"):  # advisory nudge, until memory is consulted
        _emit_context(_SEARCH_REMINDER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
