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
import re
import os

_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)

# User turns that are pure harness scaffolding, not something the user said.
_NOISE_PREFIXES = (
    "<local-command",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<ide_opened_file>",
    "<user-",
    "caveat:",
    "[request interrupted",
    "base directory for this skill",
)


def _clean(text: str) -> str:
    return _SYSTEM_REMINDER.sub("", text).strip()


def _is_noise(text: str) -> bool:
    head = text.lstrip().lower()[:40]
    return any(head.startswith(prefix) for prefix in _NOISE_PREFIXES)


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
        text = _clean(content)
        return [text] if text and not (role == "user" and _is_noise(text)) else []

    lines: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                text = _clean(block)
                if text and not (role == "user" and _is_noise(text)):
                    lines.append(text)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    text = _clean(block.get("text", ""))
                    if text and not (role == "user" and _is_noise(text)):
                        lines.append(text)
                elif btype == "tool_use" and role == "assistant":
                    action = _render_tool_use(block.get("name", ""), block.get("input", {}))
                    if action:
                        lines.append(action)
                # tool_result and thinking are intentionally dropped
    return lines


def extract_text(transcript_path: str) -> str:
    parts: list[str] = []
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
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
    except FileNotFoundError:
        return ""
    return "\n".join(parts)
