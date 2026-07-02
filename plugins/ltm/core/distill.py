"""Distil raw text into atomic, injectable facts.

Distillation is lossy compression tuned for relevance — the biggest storage,
token and recall lever (atomic facts embed far better than raw line-splits).

Strategies behind one interface (Strategy pattern):
  - HeuristicDistiller  : dependency-free, keeps short declarative lines. Cannot
    detect conflicts, so it relies on similarity-based supersession downstream.
  - ClaudeCliDistiller  : shells out to ``claude -p`` (defaults to Haiku — the
    right tier for cheap extraction).
  - HTTPDistiller       : POSTs to any OpenAI-compatible chat endpoint via stdlib
    urllib. Point it at a local Ollama / LM Studio / llama.cpp / vLLM server for
    zero-token, fully offline distillation.

The LLM distillers produce genuinely atomic facts AND explicit ``supersedes``
links — the fix for vocabulary-disjoint conflicts (Paris -> London) that
similarity can't catch. All run in the detached capture worker and fall back to
the heuristic on any failure, so capture never breaks.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

_NOISE_PREFIXES = ("http", "```", "|", ">", "<")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_NON_IDS = {"", "none", "null", "n/a", "na", "-"}  # sentinels small models emit for "nothing"

# Directive / interrogative openers that mark a user ask rather than a durable
# fact — memory records what happened, not what was requested. Kept narrow and
# directive-heavy so it doesn't eat assistant declaratives ("Is/Are/Will …");
# the endswith-"?" check is the main catch. This only guards the fallback
# heuristic — the default LLM distiller does its own filtering.
_QUESTION_OPENERS = (
    "can we", "can you", "could you", "would you", "should i", "should we",
    "what does", "what is", "what's", "how do", "how does", "why is", "why do",
    "please ", "let's ", "lets ", "yes", "okay", "ok ", "one other", "note,", "note ",
)


@dataclass
class DistilledFact:
    text: str
    supersedes: list[str] = field(default_factory=list)
    title: str = ""
    narrative: str = ""
    files: list[str] = field(default_factory=list)


def _is_user_ask(line: str) -> bool:
    lowered = line.lower()
    return line.endswith("?") or lowered.startswith(_QUESTION_OPENERS)


def _candidates(text: str):
    for raw in text.splitlines():
        line = raw.strip().strip("-*#• \t")
        if not line or line.startswith(_NOISE_PREFIXES):
            continue
        if len(line) <= 240:
            yield line
        else:
            for sentence in _SENTENCE.split(line):
                sentence = sentence.strip()
                if sentence:
                    yield sentence


def heuristic_facts(text: str, max_facts: int = 12, min_len: int = 14) -> list[str]:
    facts: list[str] = []
    seen: set[str] = set()
    for line in _candidates(text):
        if not (min_len <= len(line) <= 240) or _is_user_ask(line):
            continue
        key = " ".join(line.lower().split())
        if key in seen:
            continue
        seen.add(key)
        facts.append(line)
        if len(facts) >= max_facts:
            break
    return facts


class Distiller(ABC):
    @abstractmethod
    def distill(self, text: str, existing: list[tuple[str, str]]) -> list[DistilledFact]:
        """existing = (fact_id, fact_text) for active facts in this project."""

    def summarize(self, text: str) -> DistilledFact | None:
        """A single session-level summary fact, or None if unsupported (heuristic)."""
        return None


class HeuristicDistiller(Distiller):
    def distill(self, text: str, existing: list[tuple[str, str]]) -> list[DistilledFact]:
        return [DistilledFact(fact) for fact in heuristic_facts(text)]


_PROMPT = """You extract durable long-term memory from a coding assistant session.

The transcript interleaves user messages with the assistant's actions (rendered
as lines like "Edited auth.py", "Ran: just test") and its explanations. Record
what the ASSISTANT did and learned, not what the user asked.

Output ONLY a JSON object of the form {{"facts": [ ... ]}}. Each element:
  {{"text": "<one atomic, self-contained fact in present tense, <=200 chars>",
    "title": "<short headline, <=60 chars>",
    "narrative": "<1-3 sentences giving the why/how and any detail worth keeping, <=500 chars>",
    "files": ["<repo-relative path this fact concerns>", ...],
    "supersedes": ["<id of an existing fact this makes outdated>", ...]}}

`text` is the atomic fact used for retrieval; `title`/`narrative`/`files` add
recoverable detail. Use [] / "" when a field does not apply.

Prefer facts in these categories:
- what-changed  : a concrete change made (file/module edited, feature added, config set)
- decision      : a choice made and, briefly, why
- problem-solution / gotcha : a bug hit and how it was fixed; a non-obvious trap
- pattern / convention : a reusable approach or house style adopted
- trade-off     : an option weighed and rejected, and why

Rules:
- Capture outcomes that help a future session, not narration. Skip questions,
  chatter, tool noise, and anything transient.
- Attribute concretely ("Uses X because Y"), not vaguely ("made some changes").
- If a new fact updates or contradicts an existing one, put that fact's id in
  "supersedes" (even if the wording is completely different); else use [].

Existing facts (id: text):
{existing}

Session transcript:
{transcript}
"""


def _coerce_items(output: str) -> list:
    """Pull the fact array out of an LLM response.

    Tolerates the two shapes small models emit: a bare JSON array, or a
    ``{"facts": [...]}`` object (what ``response_format: json_object`` forces).
    Tries a strict parse first, then the widest object/array substring so a
    stray prose preamble or markdown fence doesn't defeat it.
    """
    slices = (
        output,
        output[output.find("[") : output.rfind("]") + 1],
        output[output.find("{") : output.rfind("}") + 1],
    )
    for candidate in slices:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("facts", "observations", "items", "memories"):
                if isinstance(data.get(key), list):
                    return data[key]
            for value in data.values():
                if isinstance(value, list):
                    return value
    return []


def _str_list(value) -> list[str]:
    if not isinstance(value, list):
        value = [value] if value else []
    return [str(v).strip() for v in value if str(v).strip()]


def parse_records(output: str) -> list[DistilledFact]:
    records = []
    for item in _coerce_items(output):
        if not (isinstance(item, dict) and str(item.get("text", "")).strip()):
            continue
        supersedes = [s for s in _str_list(item.get("supersedes")) if s.lower() not in _NON_IDS]
        records.append(
            DistilledFact(
                text=str(item["text"]).strip(),
                supersedes=supersedes,
                title=str(item.get("title", "")).strip(),
                narrative=str(item.get("narrative", "")).strip(),
                files=_str_list(item.get("files")),
            )
        )
    return records


def _build_prompt(text: str, existing: list[tuple[str, str]]) -> str:
    existing_block = "\n".join(f"{fid}: {ftext}" for fid, ftext in existing) or "(none)"
    return _PROMPT.format(existing=existing_block, transcript=text)


_SUMMARY_PROMPT = """Summarise this coding-assistant session as one durable memory.

Output ONLY a JSON object:
  {{"title": "<short headline of what the session was about, <=70 chars>",
    "request": "<what the user set out to achieve>",
    "learned": "<key findings, decisions, or gotchas>",
    "completed": "<what was actually done, incl. commits/files touched>",
    "next_steps": "<what remains, or empty string if nothing>"}}

Be concrete and specific; prefer names, paths and outcomes over narration.

Session transcript:
{transcript}
"""

_SUMMARY_SECTIONS = (
    ("request", "Request"),
    ("learned", "Learned"),
    ("completed", "Completed"),
    ("next_steps", "Next steps"),
)


def parse_summary(output: str) -> DistilledFact | None:
    start, end = output.find("{"), output.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(output[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    title = str(obj.get("title", "")).strip()
    narrative = "\n".join(
        f"{label}: {str(obj.get(key, '')).strip()}"
        for key, label in _SUMMARY_SECTIONS
        if str(obj.get(key, "")).strip()
    )
    text = title or str(obj.get("completed", "")).strip()[:200]
    return DistilledFact(text=text, title=title, narrative=narrative) if text else None


class ClaudeCliDistiller(Distiller):
    """Headless ``claude -p``. Defaults to Haiku — cheap and fast for extraction."""

    def __init__(self, cmd: str = "claude", model: str = "", timeout: int = 120) -> None:
        self.cmd = cmd
        self.model = model or "haiku"
        self.timeout = timeout

    def _complete(self, prompt: str) -> str:
        args = [self.cmd, "-p"]
        if self.model:
            args += ["--model", self.model]
        result = subprocess.run(
            args, input=prompt, capture_output=True, text=True, timeout=self.timeout
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "llm error")[:200])
        return result.stdout

    def distill(self, text: str, existing: list[tuple[str, str]]) -> list[DistilledFact]:
        try:
            records = parse_records(self._complete(_build_prompt(text, existing)))
            if records:
                return records
        except Exception:
            pass
        return HeuristicDistiller().distill(text, existing)

    def summarize(self, text: str) -> DistilledFact | None:
        try:
            return parse_summary(self._complete(_SUMMARY_PROMPT.format(transcript=text)))
        except Exception:
            return None


class HTTPDistiller(Distiller):
    """Any OpenAI-compatible chat endpoint (Ollama / LM Studio / llama.cpp / vLLM).

    With a local server this is zero-token and fully offline. Stdlib-only.
    """

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def _complete(self, prompt: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "You extract long-term memory. Output only a JSON object."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "stream": False,
                # Guarantees syntactically valid JSON, so a stray token can't drop the
                # whole capture to the heuristic fallback. Honoured by Ollama/vLLM/LM Studio.
                "response_format": {"type": "json_object"},
            }
        ).encode()
        request = urllib.request.Request(f"{self.base_url}/chat/completions", data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode())
        return data["choices"][0]["message"]["content"]

    def distill(self, text: str, existing: list[tuple[str, str]]) -> list[DistilledFact]:
        try:
            records = parse_records(self._complete(_build_prompt(text, existing)))
            if records:
                return records
        except Exception:
            pass
        return HeuristicDistiller().distill(text, existing)

    def summarize(self, text: str) -> DistilledFact | None:
        try:
            return parse_summary(self._complete(_SUMMARY_PROMPT.format(transcript=text)))
        except Exception:
            return None


def get_distiller(cfg) -> Distiller:
    if cfg.distiller in ("claude", "llm"):
        return ClaudeCliDistiller(cfg.distiller_cmd, cfg.distiller_model)
    if cfg.distiller in ("ollama", "http", "openai"):
        return HTTPDistiller(
            cfg.distiller_base_url,
            cfg.distiller_model or "qwen2.5:3b",
            cfg.distiller_api_key,
        )
    return HeuristicDistiller()
