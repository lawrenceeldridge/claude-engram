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

import hashlib
import json
import re
import subprocess
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

_NOISE_PREFIXES = ("http", "```", "|", ">", "<")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_NON_IDS = {"", "none", "null", "n/a", "na", "-"}  # sentinels small models emit for "nothing"

# Observation categories (mirrors claude-mem's taxonomy). Small models drift, so
# anything off-list falls back to "discovery" at parse time.
_TYPES = {"decision", "bugfix", "feature", "refactor", "discovery", "change"}
_DEFAULT_TYPE = "discovery"

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

# First-person / self-referential procedural narration the assistant emits while
# working ("Let me check …", "I'll now …", "The assistant can now …"). These are
# transient chatter, not durable project facts, and pollute recall when the
# heuristic fallback stores them verbatim. Kept narrow so it doesn't eat genuine
# declarative outcomes ("The delete dialog now uses an AlertDialog."): it only
# matches planning/self-reference openers, not every sentence starting with "the".
_NARRATION_OPENERS = (
    "let me", "let us ", "i'll ", "i will ", "i'm going to", "i am going to",
    "i've ", "i have ", "now i", "now let", "first, i", "first i", "next, i",
    "next i", "then i", "here's ", "here is ", "the assistant ", "we now ",
    "we can now",
)


@dataclass
class DistilledFact:
    text: str
    supersedes: list[str] = field(default_factory=list)
    title: str = ""
    subtitle: str = ""
    narrative: str = ""
    files: list[str] = field(default_factory=list)
    type: str = ""
    observation_id: str = ""
    degraded: bool = False  # produced by the heuristic fallback, not the LLM — eligible for re-distillation


@dataclass
class Observation:
    """A typed group of atomic facts plus one shared narrative (the card unit).

    Facts remain the embedded retrieval unit; type/title/subtitle/narrative/files
    are card metadata shared across the group.
    """

    facts: list[str]
    type: str = _DEFAULT_TYPE
    title: str = ""
    subtitle: str = ""
    narrative: str = ""
    files: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)


def _is_user_ask(line: str) -> bool:
    lowered = line.lower()
    return line.endswith("?") or lowered.startswith(_QUESTION_OPENERS)


def _is_narration(line: str) -> bool:
    """Transient assistant chatter (planning preambles, self-reference), not a durable fact."""
    return line.endswith(":") or line.lower().startswith(_NARRATION_OPENERS)


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
        if not (min_len <= len(line) <= 240) or _is_user_ask(line) or _is_narration(line):
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
        return [DistilledFact(fact, type=_DEFAULT_TYPE, degraded=True) for fact in heuristic_facts(text)]


_PROMPT = """You extract durable long-term memory from a coding assistant session.

The transcript interleaves user messages with the assistant's actions (rendered
as lines like "Edited auth.py", "Ran: just test") and its explanations. Record
what the ASSISTANT did and learned, not what the user asked.

Group related facts into observations. Output ONLY a JSON object of the form
{{"observations": [ ... ]}}. Each observation:
  {{"type": "<one of: decision|bugfix|feature|refactor|discovery|change>",
    "title": "<short headline for the group, <=60 chars>",
    "subtitle": "<one full sentence summarising the observation, <=160 chars>",
    "facts": ["<atomic, self-contained fact in present tense>", ... up to 7],
    "narrative": "<3-6 sentences of the what/why/how, with concrete detail, names, and outcomes>",
    "files": ["<repo-relative path this observation concerns>", ...],
    "supersedes": ["<id of an existing fact this observation makes outdated>", ...]}}

Each string in `facts` is an atomic memory used for retrieval, so keep each one
self-contained and specific; capture every distinct point (up to 7). `title` is a
terse headline, `subtitle` is a readable one-sentence summary, and `narrative` is
a fuller prose explanation — write all three. Use [] / "" when a field truly does
not apply. One observation may hold a single fact.

Choose `type` by intent:
- feature   : new capability or feature added
- change    : a concrete change to existing behaviour/config
- refactor  : restructuring without behaviour change
- bugfix    : a bug hit and how it was fixed
- decision  : a choice made and, briefly, why
- discovery : something learned about the code/system (default)

Rules:
- Capture outcomes that help a future session, not narration. Skip questions,
  chatter, tool noise, and anything transient.
- Attribute concretely ("Uses X because Y"), not vaguely ("made some changes").
- If an observation updates or contradicts existing facts, put their ids in
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


def parse_observations(output: str) -> list[Observation]:
    observations = []
    for item in _coerce_items(output):
        if not isinstance(item, dict):
            continue
        facts = _str_list(item.get("facts")) or ([str(item["text"]).strip()] if item.get("text") else [])
        if not facts:
            continue
        typ = str(item.get("type", "")).strip().lower()
        observations.append(
            Observation(
                facts=facts,
                type=typ if typ in _TYPES else _DEFAULT_TYPE,
                title=str(item.get("title", "")).strip(),
                subtitle=str(item.get("subtitle", "")).strip(),
                narrative=str(item.get("narrative", "")).strip(),
                files=_str_list(item.get("files")),
                supersedes=[s for s in _str_list(item.get("supersedes")) if s.lower() not in _NON_IDS],
            )
        )
    return observations


def _observation_id(obs: Observation) -> str:
    basis = obs.title + "\x00" + "\x00".join(obs.facts)
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def observations_to_facts(observations: list[Observation]) -> list[DistilledFact]:
    """Flatten observations to atomic facts sharing card metadata.

    Supersession is attached to the group's first fact only, so a group retires its
    victims once rather than once per fact.
    """
    records: list[DistilledFact] = []
    for obs in observations:
        oid = _observation_id(obs)
        for index, fact in enumerate(obs.facts):
            records.append(
                DistilledFact(
                    text=fact,
                    supersedes=obs.supersedes if index == 0 else [],
                    title=obs.title,
                    subtitle=obs.subtitle,
                    narrative=obs.narrative,
                    files=obs.files,
                    type=obs.type,
                    observation_id=oid,
                )
            )
    return records


# Cap the transcript handed to an LLM: an oversized turn otherwise exceeds the distiller
# timeout, silently dropping the whole capture to the heuristic line-splitter (untitled
# "discovery" noise). Head+tail keeps a turn's intent and outcome. Sized so a local ~14B
# model stays under a 120s budget (measured: ~24KB ≈ 30s, ~117KB times out).
_MAX_INPUT_CHARS = 24000


def _clip(text: str, limit: int = _MAX_INPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 5
    tail = limit - head
    return f"{text[:head]}\n\n…[{len(text) - limit} characters omitted]…\n\n{text[-tail:]}"


def _build_prompt(text: str, existing: list[tuple[str, str]]) -> str:
    existing_block = "\n".join(f"{fid}: {ftext}" for fid, ftext in existing) or "(none)"
    return _PROMPT.format(existing=existing_block, transcript=_clip(text))


def _build_summary_prompt(text: str) -> str:
    return _SUMMARY_PROMPT.format(transcript=_clip(text))


_SUMMARY_PROMPT = """Summarise this coding-assistant session as one durable memory.

Output ONLY a JSON object:
  {{"title": "<short headline of what the session was about, <=70 chars>",
    "investigated": "<what was explored/read/diagnosed>",
    "learned": "<key findings, decisions, or gotchas>",
    "completed": "<what was actually done, incl. commits/files touched>",
    "next_steps": "<what remains, or empty string if nothing>"}}

Be concrete and specific; prefer names, paths and outcomes over narration.

Session transcript:
{transcript}
"""

_SUMMARY_SECTIONS = (
    ("investigated", "Investigated"),
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
    return DistilledFact(text=text, title=title, narrative=narrative, type="session_summary") if text else None


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
            records = observations_to_facts(parse_observations(self._complete(_build_prompt(text, existing))))
            if records:
                return records
        except Exception:
            pass
        return HeuristicDistiller().distill(text, existing)

    def summarize(self, text: str) -> DistilledFact | None:
        try:
            return parse_summary(self._complete(_build_summary_prompt(text)))
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
            records = observations_to_facts(parse_observations(self._complete(_build_prompt(text, existing))))
            if records:
                return records
        except Exception:
            pass
        return HeuristicDistiller().distill(text, existing)

    def summarize(self, text: str) -> DistilledFact | None:
        try:
            return parse_summary(self._complete(_build_summary_prompt(text)))
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
