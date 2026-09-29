"""Episodic units — verbatim conversation exchanges (Functional Core — pure).

Distilled facts are engram's *semantic* memory: short, injected, supersedable. An exchange is
the matching *episodic* trace: one user turn with the assistant turns that answer it, kept
verbatim so the detail a fact dropped can be fetched on demand. Pure: it takes already-rendered
``(role, text)`` turns and returns text units — no transcript parsing, no I/O, no clock.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from core.domain.privacy import redact

EXCHANGE_MAX_CHARS = 800  # one exchange unit's size cap (mempalace's drawer size, for comparability)

_LABELS = {"user": "User", "assistant": "Assistant"}


def episode_key(session_id: str, start: int) -> str:
    """The episode a transcript delta forms — ``<session>:<delta start offset>``. Shared by the
    delta's exchanges (their source) and the facts distilled from it (their provenance link)."""
    return f"{session_id or 'session'}:{start}"


@dataclass(frozen=True)
class Exchange:
    """One verbatim exchange unit: ``turn`` is its exchange index, ``part`` its split index."""

    turn: int
    part: int
    text: str


def _split(text: str, max_chars: int) -> list[str]:
    """Split on line boundaries into pieces of at most ``max_chars``; hard-split any longer line."""
    pieces: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:max_chars])
            line = line[max_chars:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_chars:
            pieces.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        pieces.append(current)
    return [p for p in pieces if p.strip()]


def exchange_units(turns: Iterable[tuple[str, str]], max_chars: int = EXCHANGE_MAX_CHARS) -> list[Exchange]:
    """Group turns into exchanges — each user turn opens one; following assistant turns join it —
    then split each exchange to ``max_chars``. Assistant turns before any user turn form exchange 0.
    Roles other than user/assistant and empty turns are skipped."""
    grouped: list[list[str]] = []
    for role, text in turns:
        label = _LABELS.get(role)
        body = (text or "").strip()
        if label is None or not body:
            continue
        if role == "user" or not grouped:
            grouped.append([])
        grouped[-1].append(f"{label}: {body}")
    return [
        Exchange(turn, part, piece)
        for turn, lines in enumerate(grouped)
        for part, piece in enumerate(_split("\n".join(lines), max_chars))
    ]


def should_keep_exchange(exchange: Exchange, min_chars: int) -> bool:
    """The episodic attention gate: keep an exchange with at least ``min_chars`` of text; shorter
    trivia ("User: ok") decays with the sensory register instead of being stored. Action-only
    exchanges are kept — "what was run / edited" is itself an episodic trace."""
    return len(exchange.text) >= min_chars


def prepare_exchanges(turns: Iterable[tuple[str, str]], repo_path: str, min_chars: int) -> list[Exchange]:
    """What is stored: exchange units, each redacted, then kept only if substantive. The one
    pipeline shared by capture and the LongMemEval benchmark, so what is measured is what ships."""
    redacted = (replace(ex, text=redact(ex.text, repo_path)) for ex in exchange_units(turns))
    return [ex for ex in redacted if should_keep_exchange(ex, min_chars)]
