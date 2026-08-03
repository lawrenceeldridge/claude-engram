"""Ingestion quality gate (Functional Core — pure).

Decides whether a piece of session content is worth keeping as a durable fact. This
is the single home for the capture-time filtering that used to be scattered across
``transcript.py`` (harness-scaffolding stripping) and ``distill.py`` (user-ask /
narration gating), plus the gates the pipeline lacked: ephemeral CI/build status
lines and trivial user-prompt echoes.

Every function is a pure predicate (``str -> bool``) or a pure stripper
(``str -> str``): no I/O, no clock, no ``Config`` read. Tuning thresholds are passed
in as arguments. Every function is **total** and **fail-open** — any non-``str`` /
malformed input yields the keep-safe answer (``False`` for the "is this junk?"
predicates, i.e. *keep* it; the input unchanged for the stripper) — so a policy bug
can never make capture silently drop real content.

Called from the imperative shell (``transcript.py``, ``distill.py``, ``service.py``),
never the other way round.
"""

from __future__ import annotations

import re

# --- harness scaffolding -------------------------------------------------------
# Paired XML-ish blocks the harness injects into the transcript stream — stripped
# whole (open tag through matching close tag, non-greedy, dotall). Generalises the
# old <system-reminder>-only strip in transcript.py to also cover task-notification
# / task-prompt payloads (Door C). The \1 backreference keeps open/close matched.
_HARNESS_BLOCK = re.compile(r"<(system-reminder|task-notification|task-prompt)\b.*?</\1>", re.S)

# A USER turn whose text starts with one of these is pure harness scaffolding
# (slash-command wrapper, IDE-open notice, task-notification), not something the user
# said. Moved verbatim from transcript.py's _NOISE_PREFIXES and extended with the
# task-notification / task-prompt openers. Compared against the lower-cased head.
_HARNESS_PREFIXES = (
    "<local-command",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<ide_opened_file>",
    "<task-notification",
    "<task-prompt",
    "<user-",
    "caveat:",
    "[request interrupted",
    "base directory for this skill",
)


def strip_harness_blocks(text: str) -> str:
    """Remove paired harness blocks (``<system-reminder>…``, ``<task-notification>…``) whole.

    Generalises transcript.py's old ``<system-reminder>``-only strip. Returns the text
    with surrounding whitespace trimmed; an empty string for any non-``str`` input.
    """
    if not isinstance(text, str):
        return ""
    return _HARNESS_BLOCK.sub("", text).strip()


def is_harness_noise(text: str) -> bool:
    """True if a USER turn is pure harness scaffolding, not something the user actually said."""
    if not isinstance(text, str):
        return False
    head = text.lstrip().lower()[:40]
    return any(head.startswith(prefix) for prefix in _HARNESS_PREFIXES)


# --- user asks / assistant narration (moved from distill.py) -------------------
# Directive / interrogative openers that mark a user ask rather than a durable fact —
# memory records what happened, not what was requested. Kept narrow and directive-heavy
# so it doesn't eat assistant declaratives ("Is/Are/Will …"); the endswith-"?" check is
# the main catch. Only guards the heuristic fallback — the LLM distiller filters its own.
_QUESTION_OPENERS = (
    "can we",
    "can you",
    "could you",
    "would you",
    "should i",
    "should we",
    "what does",
    "what is",
    "what's",
    "how do",
    "how does",
    "why is",
    "why do",
    "please ",
    "let's ",
    "lets ",
    "yes",
    "okay",
    "ok ",
    "one other",
    "note,",
    "note ",
)

# First-person / self-referential procedural narration the assistant emits while working
# ("Let me check …", "I'll now …", "The assistant can now …") — transient chatter, not a
# durable project fact. Kept narrow so it doesn't eat genuine declarative outcomes ("The
# delete dialog now uses an AlertDialog."): matches planning/self-reference openers only.
_NARRATION_OPENERS = (
    "let me",
    "let us ",
    "i'll ",
    "i will ",
    "i'm going to",
    "i am going to",
    "i've ",
    "i have ",
    "now i",
    "now let",
    "first, i",
    "first i",
    "next, i",
    "next i",
    "then i",
    "here's ",
    "here is ",
    "the assistant ",
    "we now ",
    "we can now",
)


def is_user_ask(line: str) -> bool:
    """A user request/question, not a durable fact (memory records what happened)."""
    if not isinstance(line, str):
        return False
    return line.endswith("?") or line.lower().startswith(_QUESTION_OPENERS)


def is_narration(line: str) -> bool:
    """Transient assistant chatter (planning preambles, self-reference), not a durable fact."""
    if not isinstance(line, str):
        return False
    return line.endswith(":") or line.lower().startswith(_NARRATION_OPENERS)


# --- ephemeral CI / build / lint status ----------------------------------------
# Point-in-time status lines — true only at a moment, not durable knowledge ("ESLint
# passed", "production build succeeded", "working tree clean", "#291 merged"). Matched
# ONLY when the line is short AND ends on a status verdict, so a real fact that merely
# mentions a build ("the build pipeline was rewritten to use Vite") is never eaten.
_STATUS_TAIL = re.compile(
    r"\b(passed|passing|succeeded|failed|failing|errored|green|clean|merged|"
    r"up-to-date|up to date|no secrets|no issues|no errors|no warnings|nothing to commit)"
    r"[\s.!✓✅❌]*$",
    re.IGNORECASE,
)


def is_ephemeral_status(line: str, max_words: int = 6) -> bool:
    """True if a line is a transient CI/build/lint status verdict, not durable knowledge.

    Conservative by construction: only a short line (``<= max_words`` words, <= 80 chars)
    that *ends* on a status verdict qualifies, so substance-bearing facts are kept.
    """
    if not isinstance(line, str):
        return False
    stripped = line.strip()
    if not stripped or len(stripped) > 80 or len(stripped.split()) > max_words:
        return False
    return bool(_STATUS_TAIL.search(stripped))


# --- trivial user prompts ------------------------------------------------------
# Bare confirmations / directives a user types to steer a turn — real requests, but no
# durable content worth storing as a fact (they pollute recall as low-salience cues).
_TRIVIAL_CONFIRMATIONS = frozenset(
    {
        "yes", "yeah", "yep", "yup", "no", "nope", "ok", "okay", "k", "sure", "fine",
        "go", "go ahead", "do it", "proceed", "continue", "carry on", "keep going",
        "apply", "apply all", "apply it", "apply them", "commit", "commit it", "push",
        "push it", "merge", "merge it", "approve", "approved", "lgtm", "ship it",
        "thanks", "thank you", "ty", "cheers", "perfect", "great", "nice", "cool",
        "option a", "option b", "option c", "option d", "a", "b", "c", "d",
    }
)  # fmt: skip

# Short imperative/confirmation openers: a *short* prompt/fact starting with one of these is a
# directive ("Apply all", "yes lets commit", "commit the fix"), not durable content. The word
# cap protects substance — a longer text starting the same way is kept.
_TRIVIAL_STARTERS = (
    "yes", "yeah", "yep", "no", "nope", "ok", "okay", "sure",
    "apply", "commit", "push", "merge", "approve", "proceed", "continue",
    "go ", "do it", "run ", "option ", "use ",
)  # fmt: skip
_TRIVIAL_MAX_WORDS = 5

_SLASH_ECHO = re.compile(r"^/[a-z0-9][\w-]*(\s.*)?$", re.IGNORECASE)  # a pasted slash-command line


def _is_slash_or_confirmation(text: str) -> bool:
    """Unambiguous junk in BOTH a prompt and an imported fact: a pasted slash-command echo or
    an exact bare confirmation ("Option C", "lgtm", "/engram-git commit"). Length-independent
    and precise (exact-set membership, not a fuzzy opener), so it never eats a real terse fact."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return True
    if _SLASH_ECHO.match(collapsed):
        return True
    return collapsed.lower().rstrip(".!?") in _TRIVIAL_CONFIRMATIONS


def is_trivial_prompt(text: str, min_len: int = 12) -> bool:
    """True if a user *prompt* carries no durable content worth storing as a fact.

    A prompt is trivial if it is a slash-echo / exact confirmation, a short (``<=
    _TRIVIAL_MAX_WORDS``) directive opening ("yes lets commit", "run the tests"), or a
    sub-``min_len`` fragment. The fuzzy directive-opener rule is acceptable here because a
    prompt is steering, not knowledge — but it is deliberately NOT used for facts (see
    ``is_low_value_fact``). Conservative on length: a longer substantive prompt is kept.
    """
    if not isinstance(text, str):
        return False
    if _is_slash_or_confirmation(text):
        return True
    low = " ".join(text.split()).lower().rstrip(".!?")
    if len(low.split()) <= _TRIVIAL_MAX_WORDS and low.startswith(_TRIVIAL_STARTERS):
        return True
    return len(" ".join(text.split())) < min_len


# --- composite: is this worth keeping as a durable fact? -----------------------
def is_low_value_fact(text: str) -> bool:
    """True if stored/importable text is provably low-value memory, not worth keeping as a fact:
    harness scaffolding, an ephemeral CI/build status verdict, or a slash-echo / exact bare
    confirmation. Deliberately conservative — NO length threshold and NO fuzzy directive-opener,
    so a short real fact ("Use int8 vectors", "run loop detected in X") survives. The import gate
    drops irreversibly, so precision matters. Shared with the retro-sweep. Fail-open (non-str → keep).
    """
    if not isinstance(text, str):
        return False
    return is_harness_noise(text) or is_ephemeral_status(text) or _is_slash_or_confirmation(text)
