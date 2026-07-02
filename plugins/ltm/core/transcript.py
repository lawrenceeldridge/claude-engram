"""Parse a Claude Code JSONL transcript into plain conversational text.

The capture hook receives ``transcript_path`` directly on stdin, so we read that
rather than reconstructing the lossy ``~/.claude/projects/<encoded-cwd>/`` path.
"""

from __future__ import annotations

import json


def _content_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


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
                text = _content_text(message.get("content", obj.get("content")))
                if text:
                    parts.append(text)
    except FileNotFoundError:
        return ""
    return "\n".join(parts)
