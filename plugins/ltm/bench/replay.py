"""Trace-driven counterfactual: what would past search sweeps have cost via the index?

Pure functional core for ``ltm replay`` (the shell lives in ``bin/ltm``). Parses
recorded Claude Code session transcripts (JSONL), finds *search sweeps* — runs of
consecutive Grep/Glob/whole-file-Read calls that precede an answer — and, given
the index's answer for the same need, computes the counterfactual token cost.

This is the trace-driven-simulation methodology: no new sessions are run; real
recorded workloads are replayed against the retrieval surface. Credit rules are
deliberately conservative:

- only whole-file Reads count (bounded offset/limit reads are already cheap and
  ledger-credited elsewhere);
- a sweep is only *creditable* when the index answer verifiably contains the
  file the sweep landed on — "the index might have helped" earns nothing;
- the indexed-path cost includes the search outline, the fetched symbol body,
  and a fixed per-call overhead, so the alternative is never priced at zero.

Everything here is pure over parsed data — no filesystem, store, or embedder
access. The shell feeds transcripts in and index answers back.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

# Tools whose consecutive use constitutes a search sweep.
SWEEP_TOOLS = ("Grep", "Glob", "Read")
# Tokens ~ bytes/4 — the same heuristic the usage ledger uses.
BYTES_PER_TOKEN = 4
# Fixed cost of taking the indexed path: one tool call + its framing.
OVERHEAD_BYTES = 200


def parse_transcript(lines: Iterable[str]) -> list[dict]:
    """Flatten a session transcript into ordered tool-call events.

    Each event is ``{"name", "input", "id", "result_bytes"}``; result bytes are
    attached when the matching tool_result arrives. Malformed lines are skipped —
    the caller counts them via the returned events' integrity, not exceptions.
    """
    events: list[dict] = []
    by_id: dict[str, dict] = {}
    for line in lines:
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        message = record.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                event = {
                    "name": block.get("name") or "",
                    "input": block.get("input") or {},
                    "id": block.get("id") or "",
                    "result_bytes": 0,
                }
                events.append(event)
                if event["id"]:
                    by_id[event["id"]] = event
            elif block.get("type") == "tool_result":
                event = by_id.get(block.get("tool_use_id") or "")
                if event is not None:
                    event["result_bytes"] += _result_bytes(block.get("content"))
    return events


def _result_bytes(content) -> int:
    if isinstance(content, str):
        return len(content.encode("utf-8", errors="replace"))
    if isinstance(content, list):
        return sum(
            len(str(item.get("text", "")).encode("utf-8", errors="replace"))
            for item in content
            if isinstance(item, dict)
        )
    return 0


def _is_sweep_call(event: dict) -> bool:
    name = event["name"]
    if name not in SWEEP_TOOLS:
        return False
    if name == "Read":
        # Bounded reads are already the cheap path; only whole-file reads count.
        return "limit" not in event["input"]
    return True


def find_sweeps(events: list[dict], min_calls: int = 2) -> list[dict]:
    """Runs of >=min_calls consecutive sweep-tool calls, ending on a whole-file Read.

    A sweep without a landing Read is dropped: with no destination file there is
    nothing to verify a counterfactual against, so it earns no credit.
    """
    sweeps: list[dict] = []
    run: list[dict] = []

    def flush() -> None:
        if len(run) >= min_calls:
            landing = next(
                (e["input"].get("file_path") for e in reversed(run) if e["name"] == "Read"),
                None,
            )
            if landing:
                sweeps.append(
                    {
                        "calls": list(run),
                        "cost_bytes": sum(e["result_bytes"] for e in run),
                        "landing": landing,
                    }
                )
        run.clear()

    for event in events:
        if _is_sweep_call(event):
            run.append(event)
        else:
            flush()
    flush()
    return sweeps


def sweep_query(sweep: dict) -> str:
    """Infer what the sweep was looking for, from its own inputs.

    Grep/Glob patterns plus the landing file's stem — a rough proxy for the
    intent, which keeps the replay honest: the index is queried with what the
    session actually searched, not with hindsight.
    """
    terms: list[str] = []
    for event in sweep["calls"]:
        pattern = event["input"].get("pattern") or event["input"].get("path") or ""
        if pattern and pattern not in terms:
            terms.append(str(pattern))
    stem = str(sweep["landing"]).rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if stem and stem not in terms:
        terms.append(stem.replace("_", " ").replace("-", " "))
    return " ".join(terms)[:200]


def counterfactual(sweep: dict, hit_paths: list[str], outline_bytes: int, body_bytes: int) -> dict:
    """Price the sweep against the indexed path.

    Creditable only when the landing file appears among the index hits (suffix
    match, since hits carry repo-relative paths). The indexed cost charges the
    outline, the fetched body, and a per-call overhead for each of the two calls
    (search + get), so savings are a floor, not a flattering estimate.
    """
    landing = str(sweep["landing"])
    matched = any(hit and landing.endswith(hit) for hit in hit_paths)
    indexed_bytes = outline_bytes + body_bytes + 2 * OVERHEAD_BYTES
    return {
        "creditable": matched,
        "actual_tokens": sweep["cost_bytes"] // BYTES_PER_TOKEN,
        "indexed_tokens": indexed_bytes // BYTES_PER_TOKEN,
        "saved_tokens": (sweep["cost_bytes"] - indexed_bytes) // BYTES_PER_TOKEN if matched else 0,
        "landing": landing,
    }
