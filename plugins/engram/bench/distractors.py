"""Mine distractor facts from a real engram store to pad the calibration benchmark.

The bundled dataset (a few hundred facts) is far sparser than a real store, and the
recall verdict's failure mode is a *density* effect — many near-neighbours around every
query. Padding the eval store with real facts reproduces that density.

Runtime-only by design: distractors are mined from a snapshot on every run and are
**never written to the repo** — tens of thousands of real facts cannot pass the human
privacy gate ``dataset.json`` requires (see ``mine_corpus.py``). The same contamination
and privacy filters apply, but a flagged fact is dropped outright rather than queued for
review. The live store is never read directly — callers mine a ``bench.snapshot`` copy.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from bench.mine_corpus import contamination_hit
from bench.snapshot import snapshot_db
from core.domain.lexical import token_set
from core.domain.privacy import privacy_flags
from core.project import Project
from core.store import Store

MIN_LEN = 40  # same floor as mine_corpus: short fragments aren't facts
NEAR_DUP_JACCARD = 0.6  # a distractor this close to a dataset fact could answer its queries


def find_project(store: Store, ref: str) -> Project | None:
    """The project whose key or label equals ``ref`` exactly."""
    for row in store.projects():
        if ref in (row["project_key"], row["project_label"]):
            return store.project_meta(row["project_key"])
    return None


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def mine_distractors(
    store: Store, project: Project, n: int, exclude: list[str], seed: int = 0
) -> list[tuple[str, float]]:
    """Up to ``n`` ``(text, created_at)`` facts from ``project``, filtered and seeded.

    Keeps each fact's real timestamp so fused ranking's recency channel sees a realistic
    age spread. Drops: short fragments, benchmark-contaminated text, anything privacy-flagged,
    and facts lexically near-identical to an ``exclude`` (dataset) fact.
    """
    rows = [r for r in store.active_rows_for_project(project["key"]) if r["kind"] == "fact"]
    random.Random(seed).shuffle(rows)
    excluded = [token_set(text) for text in exclude]
    # An unknown repo path must flag every absolute path, not match all of them ("" prefixes all).
    repo_path = project["path"] or "\0"
    out: list[tuple[str, float]] = []
    for row in rows:
        if len(out) >= n:
            break
        text = (row["text"] or "").strip()
        if len(text) < MIN_LEN or contamination_hit(text) or privacy_flags(text, repo_path):
            continue
        tokens = token_set(text)
        if any(_jaccard(tokens, other) >= NEAR_DUP_JACCARD for other in excluded):
            continue
        out.append((text, row["created_at"]))
    return out


def load_distractors(args: argparse.Namespace, cfg, exclude: list[str]) -> list[tuple[str, float]] | None:
    """Mine ``--distractors`` from a snapshot of ``--distractor-db``; ``None`` on a bad request."""
    if args.distractors <= 0:
        return []
    if not args.distractor_project:
        print("[distractors] --distractors needs --distractor-project (a project key or label)")
        return None
    source = args.distractor_db or cfg.db_path
    if not Path(source).is_file():
        print(f"[distractors] no engram DB at {source} (pass --distractor-db)")
        return None
    with snapshot_db(source) as snapshot:
        store = Store(snapshot)
        try:
            project = find_project(store, args.distractor_project)
            if project is None:
                print(f"[distractors] no project {args.distractor_project!r} in {source}")
                return None
            mined = mine_distractors(store, project, args.distractors, exclude)
        finally:
            store.close()
    print(f"distractors: {len(mined)} mined from {project['label']} (requested {args.distractors})")
    return mined
