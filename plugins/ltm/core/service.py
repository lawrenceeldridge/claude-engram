"""High-level operations shared by hooks, the CLI and the daemon.

Write side (capture) is heavy and runs off the interactive path. Read side
(recall) is tiny and latency-critical. Capture applies the memory-lifecycle rules:
  - reinforcement : a fact seen again strengthens (frequency) instead of duplicating
  - supersession  : a new fact near-identical to older ones archives them (newest wins)
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

from core.config import Config
from core.domain.confidence import compute_confidence
from core.domain.lexical import has_overlap
from core.domain.quantize import cosine, dequantize_int8, pack_bits, quantize_int8
from core.ports.distill import (
    LLM_DISTILLERS,
    DistilledFact,
    get_distiller,
    has_admission_markers,
    is_distiller_prompt,
)
from core.ports.embedding import EmbeddingGateway
from core.ports.membus import WorkItem, get_bus
from core.project import GLOBAL_PROJECT_KEY, Project, global_project
from core.recall import render_block, render_scaffold, search, search_fused
from core.store import Store
from core.transcript import extract_incremental_parts, extract_text


def _find_superseded(store: Store, project_key: str, vec: list[float], threshold: float) -> list[str]:
    if threshold >= 1.0:
        return []
    victims = []
    for row in store.active_rows_for_project(project_key):
        if row["dim"] and row["dim"] != len(vec):
            continue
        if cosine(vec, dequantize_int8(row["vec_int8"], row["scale"])) >= threshold:
            victims.append(row["id"])
    return victims


def _resolve_supersedes(store: Store, project_key: str, refs: list[str]) -> set[str]:
    """Turn distiller supersedes references (fact ids) into valid same-project ids."""
    resolved = set()
    for ref in refs:
        row = store.get(ref)
        if row is not None and row["project_key"] == project_key:
            resolved.add(row["id"])
    return resolved


def add_records(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    session_id: str,
    records: list[DistilledFact],
    kind: str = "fact",
    tier: str = "stm",
) -> int:
    inserted = 0
    now = time.time()
    for record in records:
        fact_id = store.fact_id(project["key"], record.text)
        if store.exists(fact_id):
            # Rehearsal — a fact seen again reinforces, and once rehearsed enough
            # (frequency >= promote_after_freq) it transfers from STM to LTM.
            freq = store.reinforce(fact_id, now)
            if freq >= cfg.promote_after_freq:
                store.promote(fact_id, now)
            continue
        vec = embedder.embed_one(record.text)
        victims = _resolve_supersedes(store, project["key"], record.supersedes)
        victims.update(_find_superseded(store, project["key"], vec, cfg.supersede_threshold))
        victims.discard(fact_id)
        blob, scale = quantize_int8(vec)
        store.add(
            project=project,
            session_id=session_id,
            kind=kind,
            text=record.text,
            vec_int8=blob,
            scale=scale,
            dim=len(vec),
            vec_bits=pack_bits(vec),
            importance=min(1.0, len(record.text) / 240.0),
            created_at=now,
            title=record.title,
            subtitle=record.subtitle,
            narrative=record.narrative,
            files=record.files,
            type=record.type,
            observation_id=record.observation_id,
            tier=tier,
        )
        if victims:
            store.supersede(list(victims), fact_id)
        inserted += 1
    return inserted


def add_facts(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    session_id: str,
    facts: list[str],
    kind: str = "fact",
) -> int:
    return add_records(store, embedder, cfg, project, session_id, [DistilledFact(f) for f in facts], kind)


def capture_text(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    session_id: str,
    text: str,
) -> int:
    if is_distiller_prompt(text):
        return 0  # a nested `claude -p` distiller session captured itself — never store it
    distiller = get_distiller(cfg)
    existing = [(row["id"], row["text"]) for row in store.recent(project["key"], 50)]
    records = distiller.distill(text, existing)
    inserted = add_records(store, embedder, cfg, project, session_id, records)
    # If an LLM distiller degraded to the heuristic (unreachable / timed out), publish
    # the raw delta to the durable 'rescue' queue so a later healthy session re-distils
    # it and replaces these facts. Idempotent on the delta's content hash.
    if records and cfg.distiller in LLM_DISTILLERS and all(r.degraded for r in records):
        fact_ids = [store.fact_id(project["key"], r.text) for r in records]
        payload = json.dumps(
            {"text": text, "fact_ids": fact_ids, "session_id": session_id, "project_key": project["key"]}
        )
        bus = get_bus(cfg, store)
        try:
            bus.publish(
                WorkItem(
                    stage="rescue",
                    project_key=project["key"],
                    msg_id="rescue:" + store.fact_id(project["key"], text),
                    session_id=session_id,
                    payload=payload,
                )
            )
        finally:
            bus.close()
    return inserted


def rescue(store: Store, embedder: EmbeddingGateway, cfg: Config, *, limit: int = 3) -> int:
    """Re-distil parked degraded deltas from the durable 'rescue' queue (design §6.4).

    The durable successor to the old ``pending_redistill`` path: drains the bus, so
    retry/backoff and dead-lettering are handled by the queue rather than an ad-hoc
    attempts column. Runs at the head of every incremental capture; cheap when empty
    (no LLM call). No-op without an LLM distiller (a heuristic-only install can't
    recover). The queue is global, so a healthy session rescues deltas any session
    parked — each work item carries its own project key.
    """
    if cfg.distiller not in LLM_DISTILLERS:
        return 0
    bus = get_bus(cfg, store)
    try:
        distiller = get_distiller(cfg)
        recovered = 0
        for lease in bus.pull("rescue", limit):
            try:
                data = json.loads(lease.item.payload)
            except (ValueError, TypeError):
                lease.term()  # unparseable payload — dead-letter, never retry
                continue
            project = store.project_meta(data.get("project_key", ""))
            existing = [(row["id"], row["text"]) for row in store.recent(project["key"], 50)]
            records = distiller.distill(data.get("text", ""), existing)
            if records and not all(r.degraded for r in records):
                store.delete_facts(data.get("fact_ids") or [])
                add_records(store, embedder, cfg, project, data.get("session_id", ""), records)
                lease.ack()
                recovered += 1
            else:
                lease.nak()  # still degraded — retry later; dead-letters past bus_max_deliver
        return recovered
    finally:
        bus.close()


def capture_transcript(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    session_id: str,
    transcript_path: str,
) -> int:
    return capture_text(store, embedder, cfg, project, session_id, extract_text(transcript_path))


def capture_session_summary(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    session_id: str,
    transcript_path: str,
) -> int:
    """Distil the whole session into a single ``session_summary`` fact (idempotent).

    Runs once at SessionEnd over the full transcript — a coarse "what this session
    was about / did / learned / left" record that complements the atomic per-turn
    facts. Replaces any prior summary for the session so re-runs don't accumulate.
    """
    text = extract_text(transcript_path)
    if not text.strip():
        return 0
    summary = get_distiller(cfg).summarize(text)
    if summary is None:
        return 0
    store.clear_session_kind(project["key"], session_id, "session_summary")
    return add_records(store, embedder, cfg, project, session_id, [summary], kind="session_summary")


_SUMMARY_MIN_NEW_BYTES = 8000


def maybe_capture_summary(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    session_id: str,
    transcript_path: str,
    *,
    force: bool = False,
    min_new_bytes: int = _SUMMARY_MIN_NEW_BYTES,
) -> int:
    """Refresh the session summary, throttled by how much the transcript has grown.

    Re-summarising on every Stop would be a full-transcript LLM call per turn.
    Throttling on transcript growth (a summary cursor per session) keeps the summary
    current on Stop at a fraction of that cost, while ``force=True`` (SessionEnd /
    PreCompact checkpoints) always writes a final one. Both paths advance the cursor,
    so a checkpoint summary suppresses an immediate throttled re-run on the next Stop.
    """
    key = f"summary:{project['key']}:{session_id or transcript_path}"
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return 0
    if not force and size - store.get_capture_cursor(key) < min_new_bytes:
        return 0
    inserted = capture_session_summary(store, embedder, cfg, project, session_id, transcript_path)
    store.set_capture_cursor(key, size)
    return inserted


def capture_antipatterns(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    session_id: str,
    transcript_path: str,
) -> int:
    """Mine the session for mistakes the assistant admitted and catalogue them as durable
    ``antipattern`` facts, so a future session avoids repeating them.

    LLM-only (the heuristic distiller returns []). Records are promoted straight to LTM —
    an anti-pattern is a standing rule, not a decaying observation. Additive, not
    replace-on-rerun: the catalogue accumulates, deduped by content hash + supersession,
    so a mid-session lesson isn't lost when a later re-scan clips it from the model's input.
    Globally-scoped lessons (tool/harness) are routed to the reserved global project so they
    surface in every project's recall.
    """
    text = extract_text(transcript_path)
    if not text.strip():
        return 0
    existing = [
        (row["id"], row["text"])
        for key in (project["key"], GLOBAL_PROJECT_KEY)
        for row in store.active_antipatterns(key)
    ]
    records = get_distiller(cfg).extract_antipatterns(text, existing)
    if not records:
        return 0
    project_recs = [r for r in records if r.scope != "global"]
    global_recs = [r for r in records if r.scope == "global"]
    inserted = 0
    if project_recs:
        inserted += add_records(store, embedder, cfg, project, session_id, project_recs, kind="antipattern", tier="ltm")
    if global_recs:
        inserted += add_records(
            store, embedder, cfg, global_project(), session_id, global_recs, kind="antipattern", tier="ltm"
        )
    return inserted


_ANTIPATTERN_MIN_NEW_BYTES = 8000


def maybe_capture_antipatterns(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    session_id: str,
    transcript_path: str,
    *,
    force: bool = False,
    min_new_bytes: int = _ANTIPATTERN_MIN_NEW_BYTES,
) -> int:
    """Refresh the anti-pattern catalogue — gated by a cheap admission-marker scan and
    throttled by transcript growth (mirrors ``maybe_capture_summary``).

    Anti-patterns are far rarer than summaries, so the marker gate skips the LLM pass
    entirely on mistake-free sessions; the growth cursor stops a checkpoint pass re-running
    on the next Stop. All off the interactive path (the detached capture worker).
    """
    key = f"antipat:{project['key']}:{session_id or transcript_path}"
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return 0
    if not force and size - store.get_capture_cursor(key) < min_new_bytes:
        return 0
    # Gate: only pay for the LLM pass when the transcript plausibly admits a mistake. Advance
    # the cursor either way so a mistake-free stretch isn't re-scanned every turn.
    if not has_admission_markers(extract_text(transcript_path)):
        store.set_capture_cursor(key, size)
        return 0
    inserted = capture_antipatterns(store, embedder, cfg, project, session_id, transcript_path)
    store.set_capture_cursor(key, size)
    return inserted


def capture_prompts(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    session_id: str,
    prompts: list[str],
) -> int:
    """Store user prompts verbatim (kind='prompt') — a 1:1 copy, not distilled.

    Embedded so they stay recallable and FTS-indexed, but never superseded: a prompt
    records what was asked, not a claim that can go stale.
    """
    inserted = 0
    now = time.time()
    for prompt in prompts:
        fid = store.fact_id(project["key"], prompt)
        if store.exists(fid):
            store.reinforce(fid, now)
            continue
        vec = embedder.embed_one(prompt)
        blob, scale = quantize_int8(vec)
        store.add(
            project=project,
            session_id=session_id,
            kind="prompt",
            type="prompt",
            text=prompt,
            vec_int8=blob,
            scale=scale,
            dim=len(vec),
            vec_bits=pack_bits(vec),
            importance=0.5,
            created_at=now,
        )
        inserted += 1
    return inserted


def capture_transcript_incremental(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    session_id: str,
    transcript_path: str,
) -> int:
    """Distil only the transcript appended since this session was last captured.

    The per-turn Stop hook fires repeatedly on a growing transcript; re-distilling
    the whole thing each time is slow and — for a small local model — degrades into
    narration or hallucination. Reading just the new turns keeps each capture small,
    fast and crisp, and cheap enough to run every turn. User prompts in the delta are
    stored verbatim alongside the distilled facts. The cursor advances even when the
    delta yields no facts, so nothing is reprocessed.
    """
    rescue(store, embedder, cfg)  # drain any heuristic-fallback backlog first (durable queue)
    cursor_key = f"{project['key']}:{session_id or transcript_path}"
    start = store.get_capture_cursor(cursor_key)
    text, prompts, end = extract_incremental_parts(transcript_path, start)
    if end == start:
        return 0
    capture_prompts(store, embedder, cfg, project, session_id, prompts)
    inserted = capture_text(store, embedder, cfg, project, session_id, text) if text.strip() else 0
    store.set_capture_cursor(cursor_key, end)
    return inserted


def index_prompt_block(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    prompt: str,
) -> str:
    """Passive, hot-path-safe index nudge for the UserPromptSubmit hook.

    Surfaces the most relevant indexed code symbols / doc sections for the prompt so the
    index is consulted on *every* turn, not only when the model chooses to call
    ``search_code``. FTS-prefilters to a small candidate pool then cosine-reranks only
    those — so the cost is bounded by the pool, never the whole index (which can be tens
    of thousands of chunks). Relevance-gated by ``index_min_sim`` and byte-capped;
    returns ``""`` (Null Object) when nothing clears the gate, so irrelevant turns and
    keyword-less prompts cost zero tokens.
    """
    if cfg.index_top_k <= 0:
        return ""
    candidate_ids = store.chunk_fts_search(project["key"], prompt, limit=max(cfg.index_top_k * 6, 12))
    if not candidate_ids:
        return ""
    qvec = embedder.embed_one(prompt)
    qdim = len(qvec)
    scored: list[tuple[float, sqlite3.Row]] = []
    for cid in candidate_ids:
        row = store.get_chunk(project["key"], cid)
        if row is None or not row["vec_int8"] or (row["dim"] and row["dim"] != qdim):
            continue
        sim = cosine(qvec, dequantize_int8(row["vec_int8"], row["scale"]))
        if sim >= cfg.index_min_sim:
            scored.append((sim, row))
    if not scored:
        return ""
    scored.sort(key=lambda sr: sr[0], reverse=True)
    return _render_index_block(scored[: cfg.index_top_k], cfg.index_max_chars)


def _render_index_block(scored: list[tuple[float, sqlite3.Row]], max_chars: int) -> str:
    """One compact line per hit (path › title — summary); `get_symbol` fetches the body."""
    lines = ["Relevant indexed code/docs (use `search_code` / `get_symbol` for full source):"]
    used = 0
    for _sim, row in scored:
        label = row["title"] or row["anchor"] or ""
        summary = " ".join((row["summary"] or "").split())
        line = f"- {row['source_path']} › {label}"
        if summary:
            line += f" — {summary}"
        line = line[:200]
        if len(lines) > 1 and used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines) if len(lines) > 1 else ""


def recall_prompt_block(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    prompt: str,
) -> str:
    hits = search(store, embedder, project, prompt, cfg)
    memory, injected_ids = render_block("Relevant memory from this project:", hits, cfg.max_chars)
    index = index_prompt_block(store, embedder, cfg, project, prompt)
    block = "\n\n".join(part for part in (memory, index) if part)
    if block:  # ledger: cost side — bytes this injects into the turn (runs once per prompt)
        store.record_usage(project["key"], "inject_prompt", bytes_in=len(block))
    # Retrieval attribution — a fact injected into the turn counts as recalled, feeding
    # the testing-effect signal for the retention score and replay-based STM->LTM
    # promotion (design section 2.5 / 3A). This is the dominant recall path, so without
    # it recall_count never moves. Read-side bookkeeping, kept fail-open like the MCP
    # path (recall_structured): a write error — e.g. a busy DB during detached capture —
    # must never break recall.
    if injected_ids:
        try:
            store.mark_recalled(injected_ids)
        except sqlite3.Error:
            pass
    return block


def recall_core_block(
    store: Store,
    cfg: Config,
    project: Project,
) -> str:
    rows = store.recent(project["key"], cfg.core_size)
    hits = [(1.0, row) for row in rows]
    # The core is a stable, recency-based orientation block, not a query-driven
    # retrieval — so its ids are discarded (attributing them would inflate recall_count
    # for merely-recent facts every session and pollute the retention signal).
    header = f"Project memory ({project['label']}):"
    render = render_scaffold if cfg.core_scaffold else render_block
    block, _ids = render(header, hits, cfg.max_chars)
    if block:  # ledger: cost side — the once-per-session core injection
        store.record_usage(project["key"], "inject_core", bytes_in=len(block))
    return block


TOKENS_SAVED_PER_OK = 1200  # heuristic: an `ok` recall spares a grep + a couple of file reads
BYTES_PER_TOKEN = 4  # rough char→token ratio for the byte-accounted ledger


def usage_summary(store: Store, project_key: str | None = None) -> dict:
    """The token-savings ledger for `ltm stats` and the viewer — both sides of the budget.

    cost = bytes injected (per-prompt + session core); saved(measured) = whole-file reads
    avoided via get_symbol/get_doc_section (file - body); saved(estimated) = `ok` recalls
    × a per-search heuristic. net = saved - cost. Passive injection that merely *might*
    have prevented a search is not credited, so net is a conservative floor.
    """
    recall = store.recall_stats(project_key)
    usage = store.usage_stats(project_key)

    def _s(field: str, *kinds: str) -> int:
        return sum(usage.get(k, {}).get(field, 0) for k in kinds)

    cost = _s("bytes_in", "inject_prompt", "inject_core") // BYTES_PER_TOKEN
    # Measured saving: a targeted read of one unit instead of the whole file — via the ltm
    # tools (pull_symbol/pull_doc) OR a bounded offset/limit Read of an indexed file.
    saved_measured = _s("bytes_saved", "pull_symbol", "pull_doc", "read_bounded") // BYTES_PER_TOKEN
    ok = recall["by_verdict"].get("ok", 0)
    saved_estimated = ok * TOKENS_SAVED_PER_OK
    return {
        "recalls": recall,
        "injections": _s("n", "inject_prompt", "inject_core"),
        "targeted_reads": _s("n", "pull_symbol", "pull_doc"),
        "bounded_reads": _s("n", "read_bounded"),
        "ok_recalls": ok,
        "cost_tokens": cost,
        "saved_measured_tokens": saved_measured,
        "saved_estimated_tokens": saved_estimated,
        "net_tokens": saved_measured + saved_estimated - cost,
    }


def orientation_block(store: Store, project: Project, max_chars: int = 900) -> str:
    """The latest session summary as a 'where you left off' orientation snapshot.

    Injected at SessionStart (which also fires after a /compact), this re-establishes
    task orientation across a session or compaction boundary, delivered on the reliable
    SessionStart event (which also fires after a /compact).
    """
    row = store.latest_summary(project["key"])
    if row is None:
        return ""
    title = row["title"] or "Previous session"
    body = (row["narrative"] or row["text"] or "").strip()
    block = f"Where {project['label']} left off — {title}:\n{body}".strip()
    return block[:max_chars]


_GUIDANCE = {
    "ok": "Strong recall — trust these facts; a broad code search is likely unnecessary.",
    "low_confidence": "Weak recall — treat these as hints only; widen to Grep/Glob if they don't answer the question.",
    "no_memory": "No stored memory for this query — do not assume prior context; proceed with a normal search.",
    "embedding_mismatch": (
        "The recall embedder's vector space does not match the stored facts, so none "
        "could be compared — this is a configuration problem, NOT an empty store. The "
        "process serving recall resolved a different `embedding` backend/model than the "
        "one that wrote these facts. Align the embedding config across capture and "
        "recall (e.g. set LTM_EMBEDDING globally) and retry."
    ),
}


def _embedding_mismatch(store: Store, embedder: EmbeddingGateway, project_key: str) -> bool:
    """True when the query embedder can't compare against any stored vector.

    ``search_fused`` silently drops rows whose stored ``dim`` differs from the query
    vector's — correct for mixed stores, but indistinguishable from 'nothing stored'
    to the caller. When the project HAS active facts and none share the embedder's
    dimension, the two embedding spaces have diverged; we surface that explicitly.
    """
    qdim = getattr(embedder, "dim", None)
    if qdim is None:
        return False
    stored = store.stored_dims(project_key)
    return bool(stored) and qdim not in stored


def _pack_facts(hits: list, max_chars: int) -> tuple[list[dict], int, list[str]]:
    """Greedy budget pack: highest-ranked facts first, until the char budget is spent.

    Also returns the ids of the packed facts so the caller can record retrieval
    attribution (recall_count / last_recalled) — the id is not exposed in the DTO.
    """
    packed: list[dict] = []
    packed_ids: list[str] = []
    used = 0
    for _score, sim, row in hits:
        text = row["text"]
        if packed and used + len(text) > max_chars:
            continue
        packed.append(
            {
                "text": text,
                "similarity": round(float(sim), 4),
                "kind": row["kind"],
                "frequency": row["frequency"] or 1,
            }
        )
        packed_ids.append(row["id"])
        used += len(text)
    return packed, len(hits) - len(packed), packed_ids


def recall_structured(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    query: str,
    *,
    k: int | None = None,
    max_chars: int | None = None,
) -> dict:
    """Recall as a structured, confidence-gated result for on-demand callers.

    Unlike ``recall_prompt_block`` (which renders an injection string), this returns
    a JSON-friendly dict carrying a calibrated ``confidence`` and a ``verdict``
    (ok / low_confidence / no_memory) so the caller can decide whether to trust
    memory or fall back to a wider, more expensive search. Ranking is rank-fusion
    (``search_fused``); confidence reads the cosine similarities carried through it.
    Never raises on an empty store — it returns an explicit no_memory verdict.
    """
    max_chars = cfg.recall_max_chars if max_chars is None else max_chars
    # On-demand recall searches the broader "activated LTM" breadth (Cowan), not the small
    # injected focus; an explicit k still overrides. The injected hot path (recall_prompt_block
    # -> search -> top_k) is unaffected, so the per-turn token focus stays small.
    k = cfg.activated_k if k is None else k
    hits = search_fused(store, embedder, project, query, cfg, k=k)
    sims = [sim for _score, sim, _row in hits]
    identity = has_overlap(query, hits[0][2]["text"]) if hits else None
    confidence = compute_confidence(sims, has_identity_match=identity)["confidence"]

    if not hits:
        verdict = "embedding_mismatch" if _embedding_mismatch(store, embedder, project["key"]) else "no_memory"
    elif confidence < cfg.recall_min_confidence:
        verdict = "low_confidence"
    else:
        verdict = "ok"

    facts, dropped, recalled_ids = _pack_facts(hits, max_chars)
    result = {
        "query": query,
        "project": project["label"],
        "verdict": verdict,
        "confidence": confidence,
        "guidance": _GUIDANCE[verdict],
        "facts": facts,
        "returned": len(facts),
        "matched": len(hits),
        "dropped": dropped,
    }
    store.log_recall(
        project["key"],
        query,
        returned=len(facts),
        top_sim=sims[0] if sims else 0.0,
        confidence=confidence,
        verdict=verdict,
    )
    # Retrieval attribution feeds the retention score (testing/spacing signal). It is
    # best-effort — a failure here must never break a recall.
    try:
        store.mark_recalled(recalled_ids)
    except sqlite3.Error:
        pass
    return result
