#!/usr/bin/env python3
"""Replay real recall-ledger queries against a snapshot of the live store.

The calibration benchmark (``engram eval --confidence``) is labelled but synthetic; this
is the reality check. It re-asks the last N distinct questions the recall tool actually
received, through the production on-demand path (``search_fused_with_stats`` at
``activated_k``), on a snapshot of the configured store, and reports per candidate score:
the score spread and how often the shipped verdict says ``ok`` (through production's own
``is_trusted`` rule). It also reports how often fusion demotes the best cosine match below fused #1,
and the ages of both — the "old memories read as low confidence" symptom.

Unlabelled, so it cannot measure accuracy: a sound score is neither ~0% nor ~100% ok.
Read-only: the live DB is never opened for writing (``bench.snapshot``).

Run (from plugins/engram, with the interpreter that serves recall — e.g. the fastembed venv):
    python3 bench/replay_ledger.py --n 120
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

from bench.confidence_eval import CANDIDATES, NO_RECALL, candidate_scores  # noqa: E402
from bench.report import print_rows  # noqa: E402
from bench.snapshot import snapshot_db  # noqa: E402
from core.config import get_config  # noqa: E402
from core.ports.embedding import get_embedder  # noqa: E402
from core.recall import (  # noqa: E402
    best_match,
    get_calibration,
    is_trusted,
    recall_confidence,
    search_fused_with_stats,
)
from core.store import Store  # noqa: E402

DAY = 86400.0


def _age_days(row, now: float) -> float:
    return (now - (row["last_seen"] or row["created_at"])) / DAY


def replay(store: Store, embedder, cfg, n: int, now: float | None = None) -> dict:
    """Re-run the last ``n`` distinct ledger queries; collect scores, demotions and ages."""
    now = time.time() if now is None else now
    calibration = get_calibration(embedder)
    scores: dict[str, list[float]] = {name: [] for name in CANDIDATES}
    confidences: list[float | None] = []  # production's recall confidence as returned (None = unjudged)
    demoted = 0
    ages_fused_top: list[float] = []
    ages_best: list[float] = []
    replayed = 0
    for project_key, query in store.recent_recall_queries(n):
        result = search_fused_with_stats(
            store, embedder, store.project_meta(project_key), query, cfg, k=cfg.activated_k
        )
        if not result.hits:
            continue
        replayed += 1
        for name, value in candidate_scores(result, calibration).items():
            scores[name].append(value)
        confidences.append(recall_confidence(result, calibration))
        best = best_match(result.hits)
        demoted += best[2]["id"] != result.hits[0][2]["id"]
        ages_fused_top.append(_age_days(result.hits[0][2], now))
        ages_best.append(_age_days(best[2], now))
    return {
        "replayed": replayed,
        "scores": scores,
        "confidences": confidences,
        "demoted": demoted,
        "ages_fused_top": ages_fused_top,
        "ages_best": ages_best,
    }


def _quantiles(values: list[float]) -> tuple[float, float, float]:
    finite = [v for v in values if v != NO_RECALL]
    if len(finite) < 2:
        return (finite[0],) * 3 if finite else (0.0, 0.0, 0.0)
    deciles = statistics.quantiles(finite, n=10)
    return deciles[0], statistics.median(finite), deciles[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description="replay real recall-ledger queries on a store snapshot")
    parser.add_argument("--n", type=int, default=120, help="distinct recent ledger queries to replay")
    parser.add_argument("--db", type=Path, help="engram DB to snapshot (default: the configured store)")
    args = parser.parse_args()

    cfg = get_config()
    with snapshot_db(args.db or cfg.db_path) as snapshot:
        store = Store(snapshot)
        try:
            out = replay(store, get_embedder(cfg), cfg, args.n)
        finally:
            store.close()

    n = out["replayed"]
    if not n:
        print("no ledger queries returned facts — nothing to replay")
        return 0
    rows = []
    for name, values in out["scores"].items():
        p10, p50, p90 = _quantiles(values)
        rows.append({"candidate": name, "p10": p10, "median": p50, "p90": p90})
    print(f"replayed {n} ledger queries (k={cfg.activated_k}, embedding={cfg.embedding})\n")
    print_rows(rows, ["candidate", "p10", "median", "p90"])
    ok = sum(is_trusted(c, cfg.recall_min_confidence) for c in out["confidences"])
    print(f"\nshipped verdict ok (current >= {cfg.recall_min_confidence}): {ok}/{n} = {ok / n:.0%}")
    print(f"best cosine match NOT fused #1: {out['demoted']}/{n} = {out['demoted'] / n:.0%}")
    print(
        f"median age (days): fused #1 {statistics.median(out['ages_fused_top']):.1f}, "
        f"best cosine match {statistics.median(out['ages_best']):.1f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
