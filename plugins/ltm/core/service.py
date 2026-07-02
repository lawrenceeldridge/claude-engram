"""High-level operations shared by hooks, the CLI and the daemon.

Write side (capture) is heavy and runs off the interactive path. Read side
(recall) is tiny and latency-critical. Capture applies the memory-lifecycle rules:
  - reinforcement : a fact seen again strengthens (frequency) instead of duplicating
  - supersession  : a new fact near-identical to older ones archives them (newest wins)
"""

from __future__ import annotations

import time

from core.config import Config
from core.distill import DistilledFact, get_distiller
from core.embedding import EmbeddingGateway
from core.project import Project
from core.quantize import cosine, dequantize_int8, pack_bits, quantize_int8
from core.recall import render_block, search
from core.store import Store
from core.transcript import extract_text


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
) -> int:
    inserted = 0
    now = time.time()
    for record in records:
        fact_id = store.fact_id(project["key"], record.text)
        if store.exists(fact_id):
            store.reinforce(fact_id, now)
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
    return add_records(
        store, embedder, cfg, project, session_id, [DistilledFact(f) for f in facts], kind
    )


def capture_text(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    session_id: str,
    text: str,
) -> int:
    distiller = get_distiller(cfg)
    existing = [(row["id"], row["text"]) for row in store.recent(project["key"], 50)]
    records = distiller.distill(text, existing)
    return add_records(store, embedder, cfg, project, session_id, records)


def capture_transcript(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    session_id: str,
    transcript_path: str,
) -> int:
    return capture_text(store, embedder, cfg, project, session_id, extract_text(transcript_path))


def recall_prompt_block(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    project: Project,
    prompt: str,
) -> str:
    hits = search(store, embedder, project, prompt, cfg)
    return render_block("Relevant memory from this project:", hits, cfg.max_chars)


def recall_core_block(
    store: Store,
    cfg: Config,
    project: Project,
) -> str:
    rows = store.recent(project["key"], cfg.core_size)
    hits = [(1.0, row) for row in rows]
    return render_block(f"Project memory ({project['label']}):", hits, cfg.max_chars)
