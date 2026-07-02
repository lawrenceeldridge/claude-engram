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


class HeuristicDistiller(Distiller):
    def distill(self, text: str, existing: list[tuple[str, str]]) -> list[DistilledFact]:
        return [DistilledFact(fact) for fact in heuristic_facts(text)]


_PROMPT = """You extract durable long-term memory from a coding assistant session.

The transcript interleaves user messages with the assistant's actions (rendered
as lines like "Edited auth.py", "Ran: just test") and its explanations. Record
what the ASSISTANT did and learned, not what the user asked.

Output ONLY a JSON array. Each element:
  {{"text": "<one atomic, self-contained fact in present tense, <=200 chars>",
    "supersedes": ["<id of an existing fact this makes outdated>", ...]}}

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


def parse_records(output: str) -> list[DistilledFact]:
    start = output.find("[")
    end = output.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        items = json.loads(output[start : end + 1])
    except json.JSONDecodeError:
        return []
    records = []
    for item in items:
        if isinstance(item, dict) and str(item.get("text", "")).strip():
            supersedes = item.get("supersedes") or []
            if not isinstance(supersedes, list):
                supersedes = [supersedes]
            records.append(
                DistilledFact(
                    text=str(item["text"]).strip(),
                    supersedes=[str(s) for s in supersedes if str(s).strip().lower() not in _NON_IDS],
                )
            )
    return records


def _build_prompt(text: str, existing: list[tuple[str, str]]) -> str:
    existing_block = "\n".join(f"{fid}: {ftext}" for fid, ftext in existing) or "(none)"
    return _PROMPT.format(existing=existing_block, transcript=text)


class ClaudeCliDistiller(Distiller):
    """Headless ``claude -p``. Defaults to Haiku — cheap and fast for extraction."""

    def __init__(self, cmd: str = "claude", model: str = "", timeout: int = 120) -> None:
        self.cmd = cmd
        self.model = model or "haiku"
        self.timeout = timeout

    def distill(self, text: str, existing: list[tuple[str, str]]) -> list[DistilledFact]:
        args = [self.cmd, "-p"]
        if self.model:
            args += ["--model", self.model]
        try:
            result = subprocess.run(
                args, input=_build_prompt(text, existing), capture_output=True, text=True, timeout=self.timeout
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or "llm error")[:200])
            records = parse_records(result.stdout)
            if records:
                return records
        except Exception:
            pass
        return HeuristicDistiller().distill(text, existing)


class HTTPDistiller(Distiller):
    """Any OpenAI-compatible chat endpoint (Ollama / LM Studio / llama.cpp / vLLM).

    With a local server this is zero-token and fully offline. Stdlib-only.
    """

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def distill(self, text: str, existing: list[tuple[str, str]]) -> list[DistilledFact]:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "You extract long-term memory. Output only a JSON array."},
                    {"role": "user", "content": _build_prompt(text, existing)},
                ],
                "temperature": 0,
                "stream": False,
            }
        ).encode()
        request = urllib.request.Request(f"{self.base_url}/chat/completions", data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode())
            records = parse_records(data["choices"][0]["message"]["content"])
            if records:
                return records
        except Exception:
            pass
        return HeuristicDistiller().distill(text, existing)


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
