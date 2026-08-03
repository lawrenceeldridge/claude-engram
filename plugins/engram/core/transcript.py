"""Parse a Claude Code JSONL transcript into plain conversational text.

The capture hook receives ``transcript_path`` directly on stdin, so we read that
rather than reconstructing the lossy ``~/.claude/projects/<encoded-cwd>/`` path.

Crucially this renders what the assistant *did*, not just what was said: a
``tool_use`` block becomes an action line ("Edited auth.py", "Ran: just test"),
because the actions are the memory worth keeping. Harness scaffolding injected
into the stream (slash-command wrappers, IDE-open notices, system reminders) is
stripped — it is noise, not memory. Private reasoning (``thinking``) and verbose
``tool_result`` payloads are dropped to keep the distiller's input signal-dense.
"""

from __future__ import annotations

import json
import os

from core.domain.ingest import is_harness_noise, strip_harness_blocks

# Harness stripping/gating lives in the ingestion policy (core/domain/ingest.py) — the single
# source of truth shared with the distiller and the prompt gate. `strip_harness_blocks` removes
# paired <system-reminder>/<task-notification>/… blocks; `is_harness_noise` drops a user turn
# that is pure scaffolding (slash-command wrapper, IDE notice, task-notification).


def _short(value, limit: int = 80) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _render_tool_use(name: str, tool_input: dict) -> str:
    """Turn a tool call into a compact past-tense action line."""
    inp = tool_input if isinstance(tool_input, dict) else {}

    def base(path: str) -> str:
        return os.path.basename(str(path).rstrip("/")) or str(path)

    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"Edited {base(inp.get('file_path', '?'))}"
    if name == "Write":
        return f"Wrote {base(inp.get('file_path', '?'))}"
    if name == "Read":
        return f"Read {base(inp.get('file_path', '?'))}"
    if name == "Bash":
        return f"Ran: {_short(inp.get('command', inp.get('description', '?')))}"
    if name in ("Grep", "Glob"):
        return f"Searched for {_short(inp.get('pattern', '?'), 60)}"
    if name == "Task":
        return f"Delegated task: {_short(inp.get('description', inp.get('subagent_type', '?')), 60)}"
    if name == "WebFetch":
        return f"Fetched {_short(inp.get('url', '?'), 60)}"
    if name == "TodoWrite":
        return ""  # task-list churn is not memory
    if name.startswith("mcp__"):
        return f"Called {name}"
    return f"Used {name}: {_short(inp, 60)}"


def _content_lines(content, role: str) -> list[str]:
    if content is None:
        return []
    if isinstance(content, str):
        text = strip_harness_blocks(content)
        return [text] if text and not (role == "user" and is_harness_noise(text)) else []

    lines: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                text = strip_harness_blocks(block)
                if text and not (role == "user" and is_harness_noise(text)):
                    lines.append(text)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    text = strip_harness_blocks(block.get("text", ""))
                    if text and not (role == "user" and is_harness_noise(text)):
                        lines.append(text)
                elif btype == "tool_use" and role == "assistant":
                    action = _render_tool_use(block.get("name", ""), block.get("input", {}))
                    if action:
                        lines.append(action)
                # tool_result and thinking are intentionally dropped
    return lines


def _lines_to_parts(lines) -> list[str]:
    parts: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = obj.get("message") or {}
        role = obj.get("type") or message.get("role")
        if role not in ("user", "assistant"):
            continue
        parts.extend(_content_lines(message.get("content", obj.get("content")), role))
    return parts


def _prompt_lines(lines) -> list[str]:
    """Verbatim user prompts in the transcript — a 1:1 copy of what the user sent.

    Reuses the user-role cleaning (drops system-reminders and harness scaffolding),
    but does NOT distil: each returned string is the user's message text as typed.
    Tool-result messages (also role 'user') carry no text block, so they drop out.
    """
    prompts: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = obj.get("message") or {}
        role = obj.get("type") or message.get("role")
        if role != "user":
            continue
        text = "\n".join(_content_lines(message.get("content", obj.get("content")), "user")).strip()
        if text:
            prompts.append(text)
    return prompts


def extract_text(transcript_path: str) -> str:
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            return "\n".join(_lines_to_parts(fh))
    except FileNotFoundError:
        return ""


def _read_delta(transcript_path: str, start_offset: int) -> tuple[list[str], int]:
    """Read the transcript bytes appended since ``start_offset``; return (lines, end).

    JSONL is append-only and newline-delimited, so an end-of-content byte offset
    always lands on a line boundary. A shrunk file (rotated/truncated) resets to 0.
    Read in binary because a text-mode file can't be seeked-then-line-iterated.
    """
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return [], start_offset
    if start_offset > size:
        start_offset = 0
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(start_offset)
            data = fh.read()
    except OSError:
        return [], start_offset
    return data.decode("utf-8", errors="ignore").splitlines(), start_offset + len(data)


def extract_incremental(transcript_path: str, start_offset: int = 0) -> tuple[str, int]:
    """Assistant/user text appended since ``start_offset``. Returns (text, end_offset)."""
    lines, end = _read_delta(transcript_path, start_offset)
    return "\n".join(_lines_to_parts(lines)), end


def extract_incremental_parts(transcript_path: str, start_offset: int = 0) -> tuple[str, list[str], int]:
    """One read of the delta → (distillable_text, verbatim_user_prompts, end_offset)."""
    lines, end = _read_delta(transcript_path, start_offset)
    return "\n".join(_lines_to_parts(lines)), _prompt_lines(lines), end
