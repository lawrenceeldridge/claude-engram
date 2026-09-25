"""Throwaway eval stores for bench scenarios."""

from __future__ import annotations

from pathlib import Path

from core import service
from core.ports.distill import DistilledFact
from core.ports.embedding import EmbeddingGateway
from core.store import Store


def build_store(
    embedder: EmbeddingGateway, cfg, records: list[tuple[str, float | None]], root: Path, key: str = "eval"
) -> tuple[Store, dict]:
    """A store of ``(text, created_at)`` records (``None`` = now), embedded in batches.

    The caller decides every timestamp, so each scenario controls the age profile it
    measures. Batched ``bulk_add_records`` defers supersession, so near-duplicates survive.
    """
    store = Store(root / f"{key}.db")
    project = {"key": key, "path": str(root), "label": "eval"}
    service.bulk_add_records(store, embedder, cfg, project, "eval", [(DistilledFact(t), ts) for t, ts in records])
    return store, project
