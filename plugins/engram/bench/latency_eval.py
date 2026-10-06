"""``engram eval --latency``: time recall's read path on a snapshot of a real store.

The labelled benchmark measures *quality* on a few hundred facts; this measures *cost* at the
size real stores reach (10⁵ facts), where recall is scan-bound. It re-asks the project's own
recent ledger questions, on a snapshot (``bench.snapshot`` — the source is only opened
read-only), through the functions production calls: the hook's ``search`` and the ``recall``
tool's ``search_fused_with_stats`` at ``activated_k``, with each scorer (numpy, pure Python).

Three outputs, all repeatable on the same DB file:

- **End to end** — per-query wall time (p50 / p90 / max) per path × scorer, from clean runs.
- **Stages** — where a query's time goes (load / scan / lexical / FTS / pool / fusion), from a
  separate instrumented pass that wraps the production callables in :data:`STAGES`. The bench
  never re-implements recall, and the wrappers' overhead stays out of the end-to-end numbers.
- **A parity digest** per path × scorer — a hash of every query's ranked ids, exact scores and
  pool — so a refactor of the hot path can prove byte-identical output.

Query embedding happens once, up front, and is excluded: the model is not the cost measured
here. ``now`` is pinned to the snapshot's newest fact timestamp, so recency is deterministic.

``--latency-consolidation`` times one full ``consolidate()`` pass per stage (:data:`CONSOLIDATION_STAGES`)
on its own snapshot — consolidation writes — with the resolved config.

The whole run pins ``distiller="heuristic"``: integrate's LLM tier would otherwise send each
near-duplicate cluster to a real ``claude -p`` (the shipped default). So consolidation timings
are the store-side cost; any LLM merge time is not in them.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import statistics
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import core.consolidation.integrate as integrate_module
import core.consolidation.invalidate as invalidate_module
import core.consolidation.mature as mature_module
import core.consolidation.refine as refine_module
import core.consolidation.replay as replay_module
import core.recall as recall_module
from bench.backends import make_embedder, parse_spec
from bench.report import print_rows
from bench.snapshot import snapshot_project, store_source
from core.adapters.numpy_scorer import NumpyScorer
from core.consolidation import consolidate
from core.ports.embedding import EmbeddingGateway
from core.ports.scorer import PurePythonScorer
from core.recall import search, search_fused_with_stats
from core.store import Store

# (stage label, owner, attribute) — the production callables a recall query's time is spent in.
# Several owners may share a label (one scorer or one ranker runs per query). Time outside them
# is ``other`` (the tool's candidate loop and channel sorts, hydration).
STAGES: tuple[tuple[str, object, str], ...] = (
    ("load", recall_module, "_recall_rows"),
    ("scan", NumpyScorer, "cosine_all"),
    ("scan", PurePythonScorer, "cosine_all"),
    ("rank", recall_module, "top_by_priority"),
    ("rank", recall_module, "_score"),
    ("lexical", recall_module, "overlap_counts"),
    ("fts", Store, "fts_search"),
    ("pool", recall_module, "pool_stats"),
    ("fusion", recall_module, "fuse"),
)

# consolidate()'s stages in its order, with the key of the count it returns for each. It imports
# each stage at call time, so wrapping the module attribute is what it calls.
CONSOLIDATION_STAGES: tuple[tuple[str, object, str, str], ...] = (
    ("replay", replay_module, "replay", "promoted"),
    ("mature", mature_module, "mature", "matured"),
    ("displace", Store, "displace_stm", "displaced"),
    ("integrate", integrate_module, "integrate", "merged"),
    ("refine", refine_module, "refine", "pruned"),
    ("invalidate", invalidate_module, "invalidate_stale_antipatterns", "invalidated"),
    ("purge", Store, "purge", "purged"),
    ("forget", Store, "prune_nonfile_chunks", "forgotten"),
)

SCORERS = ("numpy", "python")
LATENCY_COLS = ["path", "scorer", "queries", "p50_ms", "p90_ms", "max_ms"]
CONSOLIDATION_COLS = ["stage", "ms", "changed"]


class PreEmbedded(EmbeddingGateway):
    """The run's query vectors, embedded once up front, so query embedding stays out of every timing."""

    def __init__(self, inner: EmbeddingGateway, queries: list[str]) -> None:
        self.dim, self.semantic = inner.dim, inner.semantic
        self._vectors = {query: inner.embed_query(query) for query in queries}

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("the latency run embeds its queries up front only")

    def embed_query(self, text: str) -> list[float]:
        return self._vectors[text]


@contextmanager
def instrument(stages: tuple[tuple, ...]) -> Iterator[dict[str, float]]:
    """Wrap each ``(label, owner, attribute, …)`` callable to add its wall time (seconds) to the
    yielded totals; restore every original on exit."""
    totals: dict[str, float] = defaultdict(float)
    originals = []
    for label, owner, attr, *_ in stages:
        raw = inspect.getattr_static(owner, attr)
        func = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw

        def timed(*args, _func=func, _label=label, **kwargs):
            start = time.perf_counter()
            try:
                return _func(*args, **kwargs)
            finally:
                totals[_label] += time.perf_counter() - start

        originals.append((owner, attr, raw))
        setattr(owner, attr, type(raw)(timed) if isinstance(raw, (staticmethod, classmethod)) else timed)
    try:
        yield totals
    finally:
        for owner, attr, raw in reversed(originals):
            setattr(owner, attr, raw)


def _hook(store, embedder, project, query, cfg, now):
    hits = search(store, embedder, project, query, cfg, now=now)
    return [(row["id"], repr(score)) for score, row in hits]


def _tool(store, embedder, project, query, cfg, now):
    result = search_fused_with_stats(store, embedder, project, query, cfg, k=cfg.activated_k)
    hits = [(row["id"], repr(fused), repr(sim)) for fused, sim, row in result.hits]
    return {"hits": hits, "pool": [result.pool.n, repr(result.pool.mean), repr(result.pool.std)]}


# The two production read paths: what the UserPromptSubmit hook and the `recall` tool call.
PATHS: dict[str, Callable] = {"hook": _hook, "tool": _tool}


def _quantile_ms(seconds: list[float], q: float) -> float:
    ordered = sorted(seconds)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))] * 1000


def run_path(path: str, store, embedder, project, queries: list[str], cfg, now: float) -> dict:
    """Clean end-to-end timings plus the parity digest of one path × scorer."""
    run = PATHS[path]
    run(store, embedder, project, queries[0], cfg, now)  # warm-up: page cache + first-call costs
    seconds, outputs = [], []
    for query in queries:
        start = time.perf_counter()
        out = run(store, embedder, project, query, cfg, now)
        seconds.append(time.perf_counter() - start)
        outputs.append([query, out])
    digest = hashlib.sha256(json.dumps(outputs, sort_keys=True).encode()).hexdigest()
    return {
        "queries": len(queries),
        "p50_ms": statistics.median(seconds) * 1000,
        "p90_ms": _quantile_ms(seconds, 0.9),
        "max_ms": max(seconds) * 1000,
        "digest": digest,
    }


def stage_breakdown(path: str, store, embedder, project, queries: list[str], cfg, now: float) -> dict[str, float]:
    """Median per-query milliseconds in each stage (``other`` = outside every stage), instrumented."""
    run = PATHS[path]
    per_stage: dict[str, list[float]] = defaultdict(list)
    with instrument(STAGES) as totals:
        for query in queries:
            totals.clear()
            start = time.perf_counter()
            run(store, embedder, project, query, cfg, now)
            elapsed = time.perf_counter() - start
            for label in dict.fromkeys(label for label, *_ in STAGES):
                per_stage[label].append(totals.get(label, 0.0))
            per_stage["other"].append(elapsed - sum(totals.values()))
    return {label: statistics.median(values) * 1000 for label, values in per_stage.items()}


def _scorer_available(scorer: str) -> bool:
    if scorer != "numpy":
        return True
    try:
        NumpyScorer()
    except Exception:
        return False
    return True


def _pinned_now(store, project) -> float:
    """The snapshot's newest fact timestamp — a clock that is the same on every run of one DB."""
    return max(row["last_seen"] or row["created_at"] for row in store.active_rows_for_project(project["key"]))


def evaluate_latency(store, project, inner: EmbeddingGateway, cfg, n: int, python_n: int) -> dict:
    """Every path × scorer on ``project``'s last ``n`` distinct ledger questions.

    The pure-Python scorer runs only the first ``python_n`` (it takes seconds per query at 10⁵
    facts). Stages are broken down on the first scorer that runs — numpy when it is installed.
    """
    queries = [q for _key, q in store.recent_recall_queries(n, project_key=project["key"])]
    if not queries:
        raise LookupError(f"no answered recall questions for {project['label']} in the ledger")
    embedder = PreEmbedded(inner, queries)
    now = _pinned_now(store, project)
    results: list[dict] = []
    stages: dict[str, dict[str, float]] = {}
    stages_scorer = None
    for scorer in SCORERS:
        if not _scorer_available(scorer):
            print(f"[latency] {scorer} scorer unavailable — skipped")
            continue
        scorer_cfg = replace(cfg, scorer=scorer)
        subset = queries[:python_n] if scorer == "python" else queries
        breakdown = stages_scorer is None
        for path in PATHS:
            timing = run_path(path, store, embedder, project, subset, scorer_cfg, now)
            results.append({"path": path, "scorer": scorer, **timing})
            if breakdown:
                stages[path] = stage_breakdown(path, store, embedder, project, subset, scorer_cfg, now)
        stages_scorer = stages_scorer or scorer
    return {
        "store": {
            "facts": store.active_count(project["key"]),
            "archived": store.archived_count(project["key"]),
            "newest": now,
        },
        "results": results,
        "stages": stages,
        "stages_scorer": stages_scorer,
    }


def evaluate_consolidation(store, project, embedder: EmbeddingGateway, cfg) -> dict:
    """One full consolidate() pass, timed per stage, on this (snapshot) store."""
    now = _pinned_now(store, project)
    with instrument(CONSOLIDATION_STAGES) as totals:
        start = time.perf_counter()
        counts = consolidate(store, cfg, project, now=now, embedder=embedder)
        elapsed = time.perf_counter() - start
    stages = [
        {"stage": label, "ms": totals.get(label, 0.0) * 1000, "changed": counts[count_key]}
        for label, _owner, _attr, count_key in CONSOLIDATION_STAGES
    ]
    return {"total_ms": elapsed * 1000, "stages": stages}


def _backend(spec: str, cfg, stored: set[int]) -> EmbeddingGateway | None:
    name, model, truncate_dim, float_mode = parse_spec(spec)
    if float_mode:
        print(f"[latency skipped {spec}] +float ranks in memory, not through the store")
        return None
    embedder = make_embedder(name, model, truncate_dim, cfg)
    if embedder.dim not in stored:
        print(f"[latency skipped {spec}] {embedder.dim}-dim queries, but the store holds {sorted(stored)}-dim vectors")
        return None
    return embedder


def _print_latency(spec: str, project, cfg, summary: dict) -> None:
    store = summary["store"]
    print(
        f"\nRecall latency — {spec}, {project['label']}: {store['facts']} active facts ({store['archived']} archived), "
        f"hook k={cfg.top_k}, tool k={cfg.activated_k}; query embedding excluded\n"
    )
    print_rows(summary["results"], LATENCY_COLS)
    for path, stages in summary["stages"].items():
        cells = ", ".join(f"{label} {ms:.0f}" for label, ms in stages.items())
        print(f"\n  {path} stages ({summary['stages_scorer']}, median ms/query, instrumented): {cells}")
    for row in summary["results"]:
        print(f"  digest {row['path']}/{row['scorer']}: {row['digest']}")


def _print_consolidation(spec: str, project, summary: dict) -> None:
    print(
        f"\nConsolidation pass — {spec}, {project['label']}: {summary['total_ms']:.0f} ms total "
        "(on a snapshot; heuristic distiller, so no LLM merge time)\n"
    )
    print_rows(summary["stages"], CONSOLIDATION_COLS)


def run_latency(cfg, backends: list[str], args: argparse.Namespace) -> int:
    """``--latency`` / ``--latency-consolidation`` for each backend whose dim matches the store."""
    cfg = replace(cfg, distiller="heuristic")  # offline: no LLM merges inside the timed consolidation
    source = store_source(args, cfg, "latency")
    if source is None:
        return 1
    report: dict = {"db": str(source), "project": args.store_project, "backends": {}}
    for spec in backends:
        entry = {}
        try:
            if args.latency:
                with snapshot_project(source, args.store_project) as (store, project):
                    embedder = _backend(spec, cfg, store.stored_dims(project["key"]))
                    if embedder is None:
                        continue
                    entry["latency"] = evaluate_latency(
                        store, project, embedder, cfg, args.latency_n, args.latency_python_n
                    )
                _print_latency(spec, project, cfg, entry["latency"])
            if args.latency_consolidation:
                with snapshot_project(source, args.store_project) as (store, project):
                    embedder = _backend(spec, cfg, store.stored_dims(project["key"]))
                    if embedder is None:
                        continue
                    entry["consolidation"] = evaluate_consolidation(store, project, embedder, cfg)
                _print_consolidation(spec, project, entry["consolidation"])
        except LookupError as exc:
            print(f"[latency] {exc}")
            return 1
        report["backends"][spec] = entry
    if args.latency_out is not None:
        Path(args.latency_out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"\nwrote {args.latency_out}")
    return 0
