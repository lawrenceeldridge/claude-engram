"""Integrate — REM-style integration: collapse near-duplicate facts into one.

The Sequential Hypothesis's REM contribution is *integrating* surviving traces rather than
just transferring or forgetting them. Two tiers, mirroring the embedding/distiller split:

- **Heuristic floor** (stdlib, always available) — cluster near-duplicate active short-term
  facts by cosine over their stored vectors and archive all but the strongest survivor
  (``status='merged'`` — reversible; recall scans 'active' only). No new text.
- **LLM tier** (opt-in, when an embedder is available and an LLM distiller is configured) —
  each cluster is sent to the distiller, which either **abstracts** it into one merged fact
  (added fresh, all members archived) or **vetoes** the merge (kept separate) — real
  integration plus a precision guard against false merges.

Runs off the hot path in the checkpoint sleep pass, *before* refine, so the retention cut
scores a deduplicated set. Gated default-off (``integrate_threshold = 0`` → no-op). The
clustering is a pure function over ``(id, vector)`` pairs; all I/O lives in the shell.
"""

from __future__ import annotations

from core.domain.quantize import cosine, dequantize_int8, pack_bits, quantize_int8
from core.ports.distill import LLM_DISTILLERS, get_distiller

# Cluster at most this many recent STM facts per pass, so the O(n²) comparison stays cheap
# off the hot path even when the active set is large. Near-duplicates collect among recent
# captures, so a recency-bounded pool is where the dedup pays off.
_CANDIDATE_CAP = 512


def cluster_duplicates(items: list[tuple[str, list[float]]], threshold: float) -> list[tuple[str, list[str]]]:
    """Greedy near-duplicate clustering by cosine ≥ ``threshold``. Pure — no I/O.

    ``items`` are ``(id, vector)`` in **survivor-preference order** (the fact that should
    survive a cluster comes first). Returns ``[(survivor_id, [absorbed_ids])]`` for clusters
    that absorbed at least one duplicate; singletons are omitted. Single-linkage against the
    survivor (not transitive chaining), so a cluster is "everything close to this survivor".
    """
    assigned: set[str] = set()
    groups: list[tuple[str, list[str]]] = []
    for i, (survivor_id, svec) in enumerate(items):
        if survivor_id in assigned:
            continue
        absorbed = [oid for oid, ovec in items[i + 1 :] if oid not in assigned and cosine(svec, ovec) >= threshold]
        if absorbed:
            assigned.add(survivor_id)
            assigned.update(absorbed)
            groups.append((survivor_id, absorbed))
    return groups


def _survivor_rank(row) -> tuple:
    """Higher = better survivor: most reinforced, then most recalled, most recent, richest."""
    return (
        row["frequency"] or 0,
        row["recall_count"] or 0,
        row["last_seen"] if row["last_seen"] is not None else (row["created_at"] or 0.0),
        len(row["text"] or ""),
    )


def integrate(store, cfg, project, now: float | None = None, embedder=None, distiller=None) -> int:
    """Collapse near-duplicate STM facts. Returns the number archived as 'merged'.

    Heuristic floor by default; the LLM tier engages only when an ``embedder`` is supplied
    and an LLM distiller is configured. ``embedder``/``distiller`` are injectable for tests;
    in production the shell passes the embedder and the distiller is built from ``cfg``.
    """
    threshold = cfg.integrate_threshold
    if threshold <= 0:
        return 0  # disabled — behaviour + eval unchanged
    rows = sorted(store.merge_candidates(project["key"], _CANDIDATE_CAP), key=_survivor_rank, reverse=True)
    items = [(row["id"], dequantize_int8(row["vec_int8"], row["scale"])) for row in rows]
    groups = cluster_duplicates(items, threshold)
    if not groups:
        return 0
    if embedder is not None and cfg.distiller in LLM_DISTILLERS:
        text_by_id = {row["id"]: row["text"] for row in rows}
        return _llm_merge(store, project, embedder, distiller or get_distiller(cfg), groups, text_by_id, now)
    # Heuristic floor: keep the strongest survivor, archive the rest as merged.
    absorbed_ids = [aid for _survivor, absorbed in groups for aid in absorbed]
    return store.set_status(absorbed_ids, "merged")


def _llm_merge(store, project, embedder, distiller, groups, text_by_id, now) -> int:
    """LLM tier: per cluster, abstract into one merged fact, veto, or (on error) fall back."""
    merged_count = 0
    for survivor_id, absorbed_ids in groups:
        members = [survivor_id, *absorbed_ids]
        try:
            merged_text = distiller.merge_cluster([text_by_id[m] for m in members])
        except Exception:
            # Fail-open — LLM unreachable/errored → blunt heuristic floor for this cluster.
            merged_count += store.set_status(absorbed_ids, "merged")
            continue
        if merged_text is None:
            continue  # LLM veto — the cluster is genuinely distinct; keep every member.
        new_id = store.fact_id(project["key"], merged_text)
        if store.exists(new_id) or new_id in members:
            # Abstraction collides with an existing fact — keep that one, archive the rest.
            # Guards against archiving the whole cluster while adding nothing.
            merged_count += store.set_status([m for m in members if m != new_id], "merged")
            continue
        vec = embedder.embed_one(merged_text)
        blob, scale = quantize_int8(vec)
        store.add(
            project=project,
            session_id="consolidate",
            kind="fact",
            text=merged_text,
            vec_int8=blob,
            scale=scale,
            dim=len(vec),
            vec_bits=pack_bits(vec),
            importance=min(1.0, len(merged_text) / 240.0),
            created_at=now,
        )
        merged_count += store.set_status(members, "merged")
    return merged_count
