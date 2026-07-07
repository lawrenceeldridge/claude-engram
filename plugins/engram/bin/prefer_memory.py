#!/usr/bin/env python3
"""PreToolUse guard — steer toward memory/index before a broad search or a raw code read.

Two behaviours, both fail-open:
  * Grep / Glob / Task / search-y Bash — a once-per-session reminder to consult recall /
    search_code / search_docs before scanning. A Bash command counts only when it's a real
    filesystem search (rg/ag/ack, grep -r, find -name), not a stdin pipe filter — so the
    common ``… | grep`` case is never touched. This closes the hole where searching via
    Bash bypassed the guard entirely.
  * Read of a large code file (>=4KB, no offset/limit) — suggest search_code + get_symbol
    instead of pulling the whole file.

Strength is set by ``ENGRAM_ENFORCE`` (default ``advisory``): ``off`` disables the guard;
``advisory`` only injects reminders and never blocks; ``strict`` turns the large-code-Read
case (indexed files only) and the Grep/Glob/Bash-search case (until memory was consulted)
into real ``deny``s — never for offset/limit reads, so a targeted pre-edit read still flows.
Pure stdlib on the common path; the strict "is it indexed?" check lazily opens the store.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

_CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_MIN_READ_BYTES = 4096

# Anti-pattern prevention (Phase 3): tools whose actions can repeat a catalogued mistake,
# and the minimum meaningful token overlap for a rule to be considered relevant (>=2 keeps
# the hot-path warning precise — a single shared common word never fires it).
_ANTIPATTERN_TOOLS = {"Bash", "Edit", "Write", "MultiEdit"}
_ANTIPATTERN_MIN_OVERLAP = 2
_ANTIPATTERN_MAX_CHARS = 600

# A Bash command that is really a filesystem/code search — the Bash equivalent of the
# Grep/Glob tools, which would otherwise slip past this guard entirely. Deliberately
# narrow (dedicated search tools, or recursive grep, or find-by-name) so ordinary pipe
# filters like `ps aux | grep foo` are NOT flagged.
_BASH_SEARCH_TOOL = re.compile(r"(?:^|[|&;]\s*|\s)(?:rg|ag|ack)\b")
_BASH_GREP_RECURSIVE = re.compile(r"\be?grep\b")
_BASH_GREP_FLAGS = re.compile(r"(?:-[A-Za-z]*[rR]\b|--include|--exclude)")
_BASH_FIND = re.compile(r"\bfind\b")
_BASH_FIND_FLAGS = re.compile(r"-(?:i?name|i?path|regex)\b")

# Destructive plugin/data command: `claude plugin uninstall|remove` deletes the plugin
# data dir (memory store + provisioned venv) unless `--keep-data` is passed. This exact
# command once wiped the whole store, so gate it before it runs.
_PLUGIN_UNINSTALL = re.compile(r"\bclaude\s+plugins?\s+(?:uninstall|remove)\b", re.I)
_KEEP_DATA = re.compile(r"--keep-data\b")


def _is_bash_search(command: str) -> bool:
    """True when a Bash command scans the filesystem for code/content (rg/ag/ack, grep -r,
    find -name) — not a stdin pipe filter."""
    if not command:
        return False
    if _BASH_SEARCH_TOOL.search(command):
        return True
    if _BASH_GREP_RECURSIVE.search(command) and _BASH_GREP_FLAGS.search(command):
        return True
    return bool(_BASH_FIND.search(command) and _BASH_FIND_FLAGS.search(command))


_SEARCH_REMINDER = (
    "claude-engram memory + index are cheaper than a broad search (measured ~2/3 fewer tokens on "
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
    marker = Path(tempfile.gettempdir()) / f"engram-{tag}-{session}.seen"
    try:
        os.close(os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def _consulted(session: str) -> bool:
    """Whether recall/search_code/search_docs has been called this session (marker from mark_consulted.py)."""
    return (Path(tempfile.gettempdir()) / f"engram-consulted-{session}.seen").exists()


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


def _tool_query(tool: str, tool_input: dict) -> str:
    """The text to match a tool action against the anti-pattern catalogue.

    Bash's command carries the richest signal (the flagship case: a mistyped CLI flag);
    for file edits the path is the cue (e.g. a rule about hand-editing generated files)."""
    if tool == "Bash":
        return str(tool_input.get("command", ""))
    return str(tool_input.get("file_path", ""))


def _uninstall_warning(tool: str, tool_input: dict) -> str | None:
    """Warn before a raw `claude plugin uninstall` that would delete the engram store.

    Fires only for our own plugin, only when `--keep-data` is absent. Pure/lexical and
    fail-open — the caller emits it as advisory context, never a block.
    """
    if tool != "Bash":
        return None
    cmd = str(tool_input.get("command", ""))
    if not _PLUGIN_UNINSTALL.search(cmd) or _KEEP_DATA.search(cmd):
        return None
    if "engram" not in cmd.lower():
        return None
    try:
        from _bootstrap import plugin_root

        plugin_root()
        from core.config import get_config

        data_dir = str(get_config().data_dir)
    except Exception:
        data_dir = "~/.claude/plugins/data/engram-<marketplace>"
    return (
        "claude-engram — STOP: a plain `claude plugin uninstall` DELETES the memory store and "
        f"provisioned venv at {data_dir} (the harness removes the plugin data dir by default). "
        "Use `engram uninstall` instead — it keeps your memory; add `--purge-data` only if you "
        "truly want it gone. Or append `--keep-data` to this command to preserve the store."
    )


def _antipattern_warning(session: str, tool: str, tool_input: dict) -> str | None:
    """A capped warning naming catalogued anti-patterns relevant to this tool action, or None.

    Lexical only (token overlap over the small anti-pattern set) — no embedding on the hot
    path. Deduped per (session, rule) so a given rule is injected at most once a session, and
    strictly fail-open: any error yields no warning and never blocks the tool.
    """
    try:
        query = _tool_query(tool, tool_input)
        if not query:
            return None
        from _bootstrap import plugin_root

        plugin_root()
        from core.config import get_config
        from core.domain.lexical import token_set
        from core.project import GLOBAL_PROJECT_KEY, resolve_project
        from core.store import Store

        cfg = get_config()
        if not cfg.antipatterns:
            return None
        qtokens = token_set(query)
        if not qtokens:
            return None
        project = resolve_project(os.getcwd(), cfg.markers)
        store = Store(cfg.db_path)
        try:
            rows = store.active_antipatterns(project["key"]) + store.active_antipatterns(GLOBAL_PROJECT_KEY)
        finally:
            store.close()
        scored = sorted(
            ((len(qtokens & token_set(row["text"])), row["id"], row["text"]) for row in rows),
            key=lambda t: t[0],
            reverse=True,
        )
        header = "claude-engram — a catalogued mistake applies to this action; avoid repeating it:"
        lines: list[str] = []
        used = len(header)
        for overlap, rid, text in scored:
            if overlap < _ANTIPATTERN_MIN_OVERLAP:
                break  # sorted desc — nothing below the floor remains
            line = f"- {text}"
            if used + len(line) + 1 > _ANTIPATTERN_MAX_CHARS:
                break
            if _once(session, f"ap-{rid}"):  # once per rule per session — bounds token cost
                lines.append(line)
                used += len(line) + 1
        return "\n".join([header, *lines]) if lines else None
    except Exception:
        return None


def main() -> int:
    from _bootstrap import hooks_disabled

    if hooks_disabled():
        return 0  # inside an engram-spawned `claude -p` — stay inert
    enforce = os.environ.get("ENGRAM_ENFORCE", "advisory").lower()
    if enforce == "off":
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    session = payload.get("session_id") or str(os.getppid())

    # Destructive-uninstall guard — highest priority: `claude plugin uninstall` without
    # --keep-data deletes the whole store, and it has. Warn (fail-open) before it runs.
    uninstall = _uninstall_warning(tool, tool_input)
    if uninstall:
        _emit_context(uninstall)
        return 0

    # Anti-pattern prevention: warn before an action that would repeat a catalogued mistake.
    # Highest-value signal, so it takes priority over the search/read nudges (a hook emits one
    # object). Fail-open and lexical-only — no embedding on the hot path.
    if tool in _ANTIPATTERN_TOOLS:
        warning = _antipattern_warning(session, tool, tool_input)
        if warning:
            _emit_context(warning)
            return 0

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
                        "This file is indexed by claude-engram. Use `search_code` + `get_symbol` to read the "
                        "specific symbol (far fewer tokens), or Read with offset/limit for a pre-edit peek. "
                        "Set ENGRAM_ENFORCE=advisory to disable this gate."
                    )
                elif _once(session, "readguard"):
                    _emit_context(_READ_ADVICE)
        return 0

    # Grep / Glob / search-y Bash / Task — enforce cheap-check-first ordering. Bash that
    # isn't a filesystem search (the common case) is none of our business — return fast.
    is_bash_search = tool == "Bash" and _is_bash_search(tool_input.get("command", ""))
    if tool == "Bash" and not is_bash_search:
        return 0

    consulted = _consulted(session)
    if (tool in ("Grep", "Glob") or is_bash_search) and not consulted and enforce == "strict":
        _emit_deny(
            "Consult claude-engram first — call `recall` and `search_code` / `search_docs` before a broad "
            "search (Grep/Glob, or grep/rg/find via Bash). Once you've checked memory/index, searches "
            "flow freely (widen when they come back weak or empty). Set ENGRAM_ENFORCE=advisory to make "
            "this a reminder instead of a gate."
        )
        return 0
    if not consulted and _once(session, "prefer"):  # advisory nudge, until memory is consulted
        _emit_context(_SEARCH_REMINDER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
