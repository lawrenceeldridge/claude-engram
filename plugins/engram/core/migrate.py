"""Import an external :class:`~core.ports.memory_source.MemorySource` into engram's store.

Write-side, offline orchestration (not a hook, not the recall path). For each project label
in the source it resolves an engram :class:`~core.project.Project` via the injected ``resolve``
callback (label → ``{path, key}``), then streams that label's records through
:func:`core.service.bulk_add_records`. A label the callback can't map is **skipped and reported**
— never written under a guessed key. ``dry_run`` counts what would import without writing.

The core stays ignorant of *how* a label maps to a path: the composition root (the ``engram
import`` CLI) supplies ``resolve`` (from ``--map`` / existing-project lookup). This keeps label
policy at the edge and the orchestration pure over the port (Dependency Inversion).
"""

from __future__ import annotations

from collections.abc import Callable

from core.config import Config
from core.domain.ingest import is_low_value_fact
from core.ports.embedding import EmbeddingGateway
from core.ports.memory_source import MemorySource
from core.project import Project
from core.service import bulk_add_records
from core.store import Store


def import_memory_source(
    store: Store,
    embedder: EmbeddingGateway,
    cfg: Config,
    source: MemorySource,
    resolve: Callable[[str], Project | None],
    *,
    only_label: str | None = None,
    dry_run: bool = False,
    session_id: str = "import",
    batch: int = 256,
    progress: Callable[[dict[str, int]], None] | None = None,
) -> dict:
    """Import records from ``source`` into the store.

    Returns ``{"available": bool, "dry_run": bool, "projects": {label: {...}}, "skipped": [label]}``.
    Each mapped project reports its target ``key`` plus either ``would_import`` (dry-run) or the
    :func:`bulk_add_records` counts (``inserted``/``reinforced``/``batches``).
    """
    if not source.available():
        return {"available": False, "dry_run": dry_run, "projects": {}, "skipped": []}

    labels = [only_label] if only_label is not None else source.project_labels()
    result: dict = {"available": True, "dry_run": dry_run, "projects": {}, "skipped": []}

    def _kept(records, counter: dict):
        """Yield source records whose fact is worth keeping, counting the drops into ``counter``.

        The ingestion quality gate applied at the import door (Door D): claude-mem and other
        sources bypass the capture-time filters, so the same policy runs here — harness blobs,
        ephemeral CI status and trivial prompt/command echoes never reach the store. Shared by
        the dry-run count and the real import so ``would_import`` matches what actually lands.
        Length-independent (``is_low_value_fact``), so a short but real imported fact is kept.
        """
        for rec in records:
            if is_low_value_fact(rec.fact.text):
                counter["skipped_low_value"] += 1
            else:
                yield rec

    for label in labels:
        project = resolve(label)
        if project is None:
            result["skipped"].append(label)
            continue
        counter = {"skipped_low_value": 0}
        if dry_run:
            would = sum(1 for _ in _kept(source.iter_records(only_label=label), counter))
            result["projects"][label] = {
                "key": project["key"],
                "would_import": would,
                "skipped_low_value": counter["skipped_low_value"],
            }
            continue
        pairs = ((rec.fact, rec.created_at_epoch) for rec in _kept(source.iter_records(only_label=label), counter))
        counts = bulk_add_records(store, embedder, cfg, project, session_id, pairs, batch=batch, progress=progress)
        result["projects"][label] = {"key": project["key"], **counts, "skipped_low_value": counter["skipped_low_value"]}
    return result
