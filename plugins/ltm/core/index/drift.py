"""Embedding-drift canary.

A long-lived vector store assumes the embedder is stable: vectors written months
apart must stay comparable. If the model (or its version) silently changes under
you, old fact vectors become incomparable to new ones and recall quietly rots.

This pins a small set of fixed strings' vectors at first use and, on demand,
re-embeds them and alarms if the mean cosine similarity to the pinned vectors
drops below a threshold — a cheap tripwire, no model internals required.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.domain.quantize import cosine
from core.ports.embedding import EmbeddingGateway

CANARY_PHRASES = [
    "the deployment pipeline runs on continuous integration",
    "authentication uses signed request headers",
    "the database stores compact quantised vectors",
    "recall injects relevant memory just in time",
    "capture distils a transcript into atomic facts",
    "the project language and build tooling are fixed",
    "supersession retires an older fact when a newer one replaces it",
    "recency decay lowers the weight of stale memories",
    "the embedding gateway is swappable behind a port",
    "similarity is measured by cosine distance",
    "reinforcement strengthens a fact seen across sessions",
    "the store is keyed by a stable project identity",
    "a hook runs off the interactive path with zero token cost",
    "the viewer serves a read-only browser over localhost",
    "rank fusion merges several scoring channels into one order",
    "confidence gates whether to trust memory or search wider",
]

DRIFT_THRESHOLD = 0.05


def _canary_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / "embedding_canary.json"


def pin(embedder: EmbeddingGateway, data_dir: Path | str, model_id: str) -> Path:
    """Record the current embedder's vectors for the canary phrases."""
    vectors = embedder.embed(CANARY_PHRASES)
    path = _canary_path(data_dir)
    path.write_text(
        json.dumps({"model": model_id, "dim": embedder.dim, "vectors": vectors}),
        encoding="utf-8",
    )
    return path


def check(embedder: EmbeddingGateway, data_dir: Path | str, model_id: str) -> dict:
    """Compare current canary embeddings to the pinned set.

    Returns a status dict. ``status`` is one of: ``unpinned`` (no baseline yet),
    ``model_changed`` / ``dim_changed`` (identity moved — always drift), ``ok`` or
    ``drift`` (mean cosine below the threshold).
    """
    path = _canary_path(data_dir)
    if not path.exists():
        return {"status": "unpinned", "hint": "Run `ltm drift` once to pin a baseline."}

    baseline = json.loads(path.read_text(encoding="utf-8"))
    if baseline.get("dim") != embedder.dim:
        return {
            "status": "dim_changed",
            "pinned_dim": baseline.get("dim"),
            "current_dim": embedder.dim,
            "hint": "Embedding dimension changed — re-embed the store.",
        }
    if baseline.get("model") != model_id:
        return {
            "status": "model_changed",
            "pinned_model": baseline.get("model"),
            "current_model": model_id,
            "hint": "Embedding model changed — old vectors are not comparable; re-embed the store.",
        }

    current = embedder.embed(CANARY_PHRASES)
    sims = [cosine(a, b) for a, b in zip(baseline["vectors"], current)]
    mean_sim = sum(sims) / len(sims) if sims else 0.0
    drifted = mean_sim < (1.0 - DRIFT_THRESHOLD)
    return {
        "status": "drift" if drifted else "ok",
        "mean_similarity": round(mean_sim, 4),
        "threshold": 1.0 - DRIFT_THRESHOLD,
        "model": model_id,
        "hint": "Embeddings drifted from the baseline — consider re-embedding the store." if drifted else "",
    }
