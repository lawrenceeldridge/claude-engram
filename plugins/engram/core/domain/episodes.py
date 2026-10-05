"""Episodic units — verbatim conversation exchanges (Functional Core — pure).

Distilled facts are engram's *semantic* memory: short, injected, supersedable. An exchange is
the matching *episodic* trace: one user turn with the assistant turns that answer it, kept
verbatim so the detail a fact dropped can be fetched on demand. Pure: it takes already-rendered
``(role, text)`` turns and returns text units — no transcript parsing, no I/O, no clock.

**Conversation first, actions as data.** The conversation is the exchange's body; the tool
actions it triggered (``ACTION_ROLE`` turns) are folded into one capped footer line on its first
part — "what was done" stays with the question that asked for it, and a long tool run no longer
spills into parts that hold nothing but "Ran: …".
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace

from core.domain.ingest import ActionVerb, action_line, parse_action
from core.domain.privacy import redact

EXCHANGE_MAX_CHARS = 800  # one exchange's conversation-text cap per part (mempalace's drawer size)
ACTION_ROLE = "action"  # a turn holding one rendered tool action (core/transcript.py emits them)
FOOTER_LABEL = "Actions: "
FOOTER_MAX_CHARS = 1024  # measured on a live store: grouped footers ran median 200 / p90 539 / max 1,124
FOOTER_SHOWN = 3  # a counted verb (commands, queries) shows this many distinct arguments
_FOOTER_TAIL_RESERVE = 16  # room for " (+NNNNN more)"

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


def _group_actions(actions: Iterable[str]) -> list[tuple[ActionVerb | None, list[str]]]:
    """Actions grouped by verb in first-use order; an unparseable line is its own group (verb None)."""
    groups: dict[str, tuple[ActionVerb | None, list[str]]] = {}
    for line in actions:
        parsed = parse_action(line, strict=False)
        verb, argument = parsed if parsed else (None, line)
        groups.setdefault(verb.verb if verb else line, (verb, []))[1].append(argument)
    return list(groups.values())


def _render_group(verb: ActionVerb | None, arguments: list[str]) -> str:
    distinct = list(dict.fromkeys(arguments))
    if verb is None:
        return ", ".join(distinct)
    if verb.listed:
        return f"{verb.verb} {', '.join(distinct)}"
    if len(arguments) == 1:
        return verb.prefix + arguments[0]
    shown = distinct[:FOOTER_SHOWN]
    more = len(arguments) - len(shown)
    return f"{verb.verb} {len(arguments)}: {'; '.join(shown)}" + (f" (+{more})" if more else "")


def action_footer(actions: Iterable[str], max_chars: int = FOOTER_MAX_CHARS) -> str:
    """One line for an exchange's tool actions, grouped by verb in first-use order —
    ``Actions: Edited a.py, b.py · Ran 12: pytest -q; git status; ruff check . (+9) · Called mcp__x``.
    A listed verb names each thing once; a counted verb gives its count and first few arguments.
    Capped at ``max_chars``: once a group doesn't fit, it (or, for a listed verb, its remaining
    names) and every later group is left out and the tail counts the actions unshown —
    ``(+N more)``. ``""`` when there are no actions."""
    groups = _group_actions(a for a in actions if a)
    if not groups:
        return ""
    full = FOOTER_LABEL + " · ".join(_render_group(verb, args) for verb, args in groups)
    if len(full) <= max_chars:
        return full
    budget = max_chars - _FOOTER_TAIL_RESERVE
    kept: list[str] = []
    omitted = 0
    for index, (verb, args) in enumerate(groups):
        fitted = _fit_group(verb, args, kept, budget)
        if fitted is None:
            omitted += sum(len(rest) for _verb, rest in groups[index:])
            break
        text, left_out = fitted
        kept.append(text)
        if left_out:
            omitted += left_out + sum(len(rest) for _verb, rest in groups[index + 1 :])
            break
    return FOOTER_LABEL + " · ".join(kept) + (" " if kept else "") + f"(+{omitted} more)"


def _fit_group(verb: ActionVerb | None, args: list[str], kept: list[str], budget: int) -> tuple[str, int] | None:
    """The longest rendering of a group that keeps the footer within ``budget`` — the whole group,
    else (listed verbs only) its first names — with how many of its actions that leaves out."""

    def fits(text: str) -> bool:
        return len(FOOTER_LABEL + " · ".join([*kept, text])) <= budget

    whole = _render_group(verb, args)
    if fits(whole):
        return whole, 0
    if verb is not None and not verb.listed:
        return None
    distinct = list(dict.fromkeys(args))
    for count in range(len(distinct) - 1, 0, -1):
        names = set(distinct[:count])
        text = _render_group(verb, distinct[:count])
        if fits(text):
            return text, sum(1 for a in args if a not in names)
    return None


def exchange_units(turns: Iterable[tuple[str, str]], max_chars: int = EXCHANGE_MAX_CHARS) -> list[Exchange]:
    """Group turns into exchanges — each user turn opens one; following assistant and action turns
    join it — then split each exchange's conversation to ``max_chars`` and append its actions'
    footer (``action_footer``) to the first part. Turns before any user turn form exchange 0.
    An exchange with no conversation text (only actions) forms no unit; unknown roles and empty
    turns are skipped."""
    grouped: list[tuple[list[str], list[str]]] = []
    for role, text in turns:
        body = (text or "").strip()
        label = _LABELS.get(role)
        if not body or (label is None and role != ACTION_ROLE):
            continue
        if role == "user" or not grouped:
            grouped.append(([], []))
        lines, actions = grouped[-1]
        if role == ACTION_ROLE:
            actions.append(body)
        else:
            lines.append(f"{label}: {body}")
    units: list[Exchange] = []
    for turn, (lines, actions) in enumerate(grouped):
        pieces = _split("\n".join(lines), max_chars)
        if not pieces:
            continue
        footer = action_footer(actions)
        if footer:
            pieces[0] = f"{pieces[0]}\n{footer}"
        units.extend(Exchange(turn, part, piece) for part, piece in enumerate(pieces))
    return units


def should_keep_exchange(exchange: Exchange, min_chars: int) -> bool:
    """The episodic attention gate: keep an exchange part with at least ``min_chars`` of text (its
    actions' footer included); shorter trivia ("User: ok") decays with the sensory register instead
    of being stored. Actions alone never make a unit — ``exchange_units`` keeps them only as the
    footer on a conversational part, so "what was run / edited" stays an episodic trace without
    parts that hold nothing else."""
    return len(exchange.text) >= min_chars


# The pre-footer fallback rendering of an unlisted tool — "Used Tool: {…argument dump…}".
_LEGACY_USED = re.compile(r"Used (\w+): ")


def _legacy_action(line: str) -> str | None:
    if parse_action(line):
        return line
    match = _LEGACY_USED.match(line)
    return action_line("Used", match.group(1)) if match else None


def legacy_turns(text: str) -> list[tuple[str, str]]:
    """Read one exchange stored before actions were folded back into turns for ``exchange_units``.

    A ``User: `` / ``Assistant: `` line opens that role's turn (an unlabelled start — a continuation
    whose first part was gated out — is the assistant's). Inside the assistant's turn a line that
    reads as an action (strict ``parse_action``, or the old ``Used <tool>: {…}`` dump, which
    becomes ``Used <tool>``) is an ``ACTION_ROLE`` turn. The one-off rewrite's reader only: live
    capture types its lines at the source.
    """
    turns: list[tuple[str, str]] = []
    role, lines = "assistant", []
    for line in text.split("\n"):
        for candidate, label in _LABELS.items():
            if line.startswith(f"{label}: "):
                if lines:
                    turns.append((role, "\n".join(lines)))
                role, lines, line = candidate, [], line[len(label) + 2 :]
                break
        action = _legacy_action(line) if role == "assistant" else None
        if action is None:
            lines.append(line)
            continue
        if lines:
            turns.append((role, "\n".join(lines)))
            lines = []
        turns.append((ACTION_ROLE, action))
    if lines:
        turns.append((role, "\n".join(lines)))
    return turns


def refold_exchanges(stored: Iterable[tuple[int, int, str]], min_chars: int) -> list[Exchange] | None:
    """One episode's stored exchanges ``(turn, part, text)`` rewritten in the current format, or
    ``None`` when there is nothing to rewrite — a footer already present, or no bare action line —
    so the rewrite is idempotent. A turn's parts are re-joined, re-read (``legacy_turns``),
    re-folded and re-split, then gated as capture gates them; turn numbers are kept and parts
    renumbered. The text was redacted when stored; nothing new is added."""
    by_turn: dict[int, list[str]] = {}
    for turn, _part, text in sorted(stored):
        by_turn.setdefault(turn, []).append(text)
    joined = {turn: "\n".join(parts) for turn, parts in by_turn.items()}
    if any(line.startswith(FOOTER_LABEL) for text in joined.values() for line in text.split("\n")):
        return None
    rebuilt = {turn: legacy_turns(text) for turn, text in joined.items()}
    if not any(role == ACTION_ROLE for turns in rebuilt.values() for role, _text in turns):
        return None
    exchanges = [
        Exchange(turn, part, unit.text)
        for turn, turns in sorted(rebuilt.items())
        for part, unit in enumerate(exchange_units(turns))
    ]
    return [ex for ex in exchanges if should_keep_exchange(ex, min_chars)]


def prepare_exchanges(turns: Iterable[tuple[str, str]], repo_path: str, min_chars: int) -> list[Exchange]:
    """What is stored: exchange units, each redacted, then kept only if substantive. The one
    pipeline shared by capture and the LongMemEval benchmark, so what is measured is what ships."""
    redacted = (replace(ex, text=redact(ex.text, repo_path)) for ex in exchange_units(turns))
    return [ex for ex in redacted if should_keep_exchange(ex, min_chars)]
