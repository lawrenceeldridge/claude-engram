#!/usr/bin/env python3
"""Compare embedding backends on a labelled recall set.

Measures retrieval quality (Recall@1, Recall@3, MRR@10) and operational cost
(corpus embed time, per-query latency, bytes/fact). Quantized runs go through the
real store path; ``+float`` runs rank on raw full-precision vectors in memory, so
the gap between a backend and its ``+float`` twin is exactly the int8 loss.

Backend spec: ``name[@model][+float]``. Examples:
    hash
    fastembed
    fastembed+float
    fastembed@BAAI/bge-base-en-v1.5

Run:
    python3 bench/run_eval.py --backends hash,fastembed,fastembed+float
    python3 bin/engram eval --backends hash,fastembed
    python3 bin/engram eval --backends hash,fastembed --confidence   # recall-verdict calibration
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

from bench.age_eval import run_aged  # noqa: E402
from bench.backends import make_embedder, parse_spec  # noqa: E402
from bench.cli_args import add_eval_arguments  # noqa: E402
from bench.confidence_eval import run_confidence  # noqa: E402
from bench.distractors import load_distractors  # noqa: E402
from bench.report import print_rows  # noqa: E402
from bench.retrieval import score_queries, search_ranker  # noqa: E402
from bench.stats import bootstrap_ci, mcnemar_exact, wilson  # noqa: E402
from core import service  # noqa: E402
from core.config import get_config  # noqa: E402
from core.consolidation.integrate import integrate  # noqa: E402
from core.domain.quantize import cosine  # noqa: E402
from core.ports.distill import DistilledFact  # noqa: E402
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.project import global_project  # noqa: E402
from core.store import Store  # noqa: E402

DATASET = Path(__file__).resolve().parent / "dataset.json"


def evaluate(spec: str, data: dict, base_cfg) -> dict:
    name, model, truncate_dim, float_mode = parse_spec(spec)
    cfg = replace(base_cfg, supersede_threshold=1.0, top_k=10, min_sim=-1.0)
    embedder = make_embedder(name, model, truncate_dim, cfg)
    facts, queries = data["facts"], data["queries"]
    embedder.embed_query("warm up the model")  # exclude cold load from timings

    if float_mode:
        start = time.perf_counter()
        fact_vecs = embedder.embed(facts)
        embed_ms = (time.perf_counter() - start) * 1000

        def rank_fn(query: str) -> list[str]:
            qv = embedder.embed_query(query)
            scored = sorted(
                ((cosine(qv, fv), text) for fv, text in zip(fact_vecs, facts)),
                key=lambda pair: pair[0],
                reverse=True,
            )
            return [text for _score, text in scored]

        bytes_per_fact = embedder.dim * 4
        store = None
    else:
        tmp = tempfile.mkdtemp(prefix="engram-bench-")
        store = Store(Path(tmp) / "eval.db")
        project = {"key": f"eval-{name}", "path": tmp, "label": "eval"}
        start = time.perf_counter()
        service.add_facts(store, embedder, cfg, project, "eval", facts)
        embed_ms = (time.perf_counter() - start) * 1000
        rows = store.active_rows_for_project(project["key"])
        bytes_per_fact = (
            sum(len(r["vec_int8"]) + (len(r["vec_bits"]) if r["vec_bits"] else 0) for r in rows) / len(rows)
            if rows
            else 0
        )
        rank_fn = search_ranker(store, embedder, project, cfg)

    r1, r3, mrr, query_ms, per_query = score_queries(queries, facts, rank_fn)
    if store is not None:
        store.close()
    return {
        "backend": spec,
        "dim": embedder.dim,
        "recall@1": r1,
        "recall@3": r3,
        "mrr@10": mrr,
        "embed_ms/fact": embed_ms / len(facts),
        "query_ms": query_ms,
        "bytes/fact": bytes_per_fact,
        "n": len(queries),
        "per_query": per_query,
    }


BACKEND_COLS = ["backend", "dim", "recall@1", "recall@3", "mrr@10", "embed_ms/fact", "query_ms", "bytes/fact"]
STM_COLS = ["stm_recall_weight", "stm_recall@1", "stm_recall@3", "stm_mrr@10"]
ANTIPATTERN_COLS = ["scope", "recall@1", "recall@3", "mrr@10"]
INTEGRATE_COLS = [
    "threshold",
    "facts_before",
    "facts_after",
    "merged",
    "recall@3_before",
    "recall@3_after",
    "recall_preserved",
]


def _print_ci(results: list[dict]) -> None:
    """Report the Wilson 95% interval on the headline proportions, so between-backend
    deltas are read against the sample's resolution (see the engram-design statistics ref)."""
    if not results:
        return
    print("\n95% Wilson intervals (proportion metrics):")
    for r in results:
        n = r.get("n", 0)
        parts = []
        for metric in ("recall@1", "recall@3"):
            lo, hi = wilson(r[metric] * n, n)
            parts.append(f"{metric} {r[metric]:.3f} [{lo:.3f}, {hi:.3f}]")
        print(f"  {r['backend']} (n={n}): " + "  ".join(parts))


def _print_pairwise(results: list[dict]) -> None:
    """Paired per-query comparisons: McNemar exact on the hit metrics, seeded
    bootstrap on the MRR delta. Backends answer identical queries, so pairing
    cancels between-query variance and resolves smaller deltas than the
    independent Wilson intervals above."""
    paired = [r for r in results if r.get("per_query")]
    if len(paired) < 2:
        return
    print("\nPaired comparisons (positive delta favours the second backend; * = p<0.05 / CI excludes 0):")
    for a, b in itertools.combinations(paired, 2):
        rows = list(zip(a["per_query"], b["per_query"]))
        print(f"  {a['backend']} -> {b['backend']}:")
        for metric, label in (("hit1", "recall@1"), ("hit3", "recall@3")):
            only_a = sum(1 for x, y in rows if x[metric] and not y[metric])
            only_b = sum(1 for x, y in rows if y[metric] and not x[metric])
            delta = (only_b - only_a) / len(rows)
            p = mcnemar_exact(only_a, only_b)
            mark = "*" if p < 0.05 else ""
            print(f"    d{label} {delta:+.3f}  (discordant {only_a}/{only_b}, p={p:.3f}){mark}")
        deltas = [y["rr"] - x["rr"] for x, y in rows]
        lo, hi = bootstrap_ci(deltas)
        mark = "*" if lo > 0 or hi < 0 else ""
        print(f"    dmrr@10   {sum(deltas) / len(deltas):+.3f}  [{lo:+.3f}, {hi:+.3f}]{mark}")


def evaluate_stm(data: dict, base_cfg, weights: tuple[float, ...] = (1.0, 0.5, 0.0)) -> list[dict]:
    """Measure the ``stm_recall_weight`` lever end-to-end on the STM scenario.

    Builds the scenario store once per weight (hash embedder — deterministic, no network),
    promotes the ``ltm_indices`` facts to the long-term tier so each fresh STM gold fact
    faces an older LTM competitor, and reports recall of the STM gold facts. As the weight
    falls, STM recall must fall too — the measurable prerequisite for any STM-ranking default
    change (design: STM is a state, not a faster clock)."""
    scenario = data.get("stm_scenario")
    if not scenario:
        return []
    facts, queries = scenario["facts"], scenario["queries"]
    ltm_indices = set(scenario.get("ltm_indices", []))
    rows_out = []
    for weight in weights:
        # supersede_threshold=1.0 keeps each STM fact and its near-duplicate LTM competitor
        # both alive (else capture would retire one); min_sim=-1 ranks the full set.
        cfg = replace(base_cfg, supersede_threshold=1.0, top_k=10, min_sim=-1.0, stm_recall_weight=weight)
        embedder = HashEmbedding(dim=cfg.dim)
        tmp = tempfile.mkdtemp(prefix="engram-bench-stm-")
        store = Store(Path(tmp) / "stm.db")
        project = {"key": f"eval-stm-{weight}", "path": tmp, "label": "eval"}
        service.add_facts(store, embedder, cfg, project, "eval", facts)
        for index, text in enumerate(facts):
            if index in ltm_indices:
                store.db.execute("UPDATE facts SET tier='ltm' WHERE id=?", (store.fact_id(project["key"], text),))
        store.db.commit()

        r1, r3, mrr, _, _ = score_queries(queries, facts, search_ranker(store, embedder, project, cfg))
        store.close()
        rows_out.append({"stm_recall_weight": weight, "stm_recall@1": r1, "stm_recall@3": r3, "stm_mrr@10": mrr})
    return rows_out


def evaluate_antipatterns(data: dict, base_cfg) -> list[dict]:
    """Measure that globally-scoped anti-patterns surface cross-project via the recall union.

    Stores the anti-patterns under the reserved global key and unrelated distractors under a
    separate project, then recalls from that project (hash embedder — deterministic, no
    network). A hit proves a tool/harness lesson captured elsewhere reaches this project.
    Recall must stay high; a regression means the global union or its ranking broke."""
    scenario = data.get("antipattern_scenario")
    if not scenario:
        return []
    antipatterns = scenario["antipatterns"]
    distractors = scenario.get("distractors", [])
    queries = scenario["queries"]
    cfg = replace(base_cfg, supersede_threshold=1.0, top_k=10, min_sim=-1.0)
    embedder = HashEmbedding(dim=cfg.dim)
    tmp = tempfile.mkdtemp(prefix="engram-bench-ap-")
    store = Store(Path(tmp) / "ap.db")
    project = {"key": "eval-ap-proj", "path": tmp, "label": "eval"}
    service.add_facts(store, embedder, cfg, project, "eval", distractors)  # local noise only
    service.add_records(
        store,
        embedder,
        cfg,
        global_project(),
        "eval",
        [DistilledFact(text=a, type="antipattern", scope="global") for a in antipatterns],
        kind="antipattern",
        tier="ltm",
    )

    r1, r3, mrr, _, _ = score_queries(queries, antipatterns, search_ranker(store, embedder, project, cfg))
    store.close()
    return [{"scope": "global", "recall@1": r1, "recall@3": r3, "mrr@10": mrr}]


def evaluate_integrate(data: dict, base_cfg) -> list[dict]:
    """Measure the integrate (gist-chunking) stage — Idea #3.

    Builds a store with a near-duplicate cluster plus distinct facts (hash embedder —
    deterministic, no network), runs the heuristic integrate floor at the scenario
    threshold, and checks the cluster collapses to one survivor (active count drops)
    while recall of the cluster's answer is preserved. A pass means dedup reduces
    redundant injection without losing the fact."""
    scenario = data.get("duplicate_cluster_scenario")
    if not scenario:
        return []
    facts, queries = scenario["facts"], scenario["queries"]
    threshold = scenario.get("threshold", 0.9)
    # heuristic distiller => integrate uses the stdlib floor (no LLM); keep near-dups alive at capture.
    cfg = replace(
        base_cfg, integrate_threshold=threshold, supersede_threshold=1.0, top_k=10, min_sim=-1.0, distiller="heuristic"
    )
    embedder = HashEmbedding(dim=cfg.dim)
    tmp = tempfile.mkdtemp(prefix="engram-bench-int-")
    store = Store(Path(tmp) / "int.db")
    project = {"key": "eval-integrate", "path": tmp, "label": "eval"}
    service.add_facts(store, embedder, cfg, project, "eval", facts)
    rank_fn = search_ranker(store, embedder, project, cfg)

    before = len(store.active_rows_for_project(project["key"]))
    r1_before, r3_before, _, _, _ = score_queries(queries, facts, rank_fn)
    merged = integrate(store, cfg, project, embedder=embedder)
    after = len(store.active_rows_for_project(project["key"]))
    r1_after, r3_after, _, _, _ = score_queries(queries, facts, rank_fn)
    store.close()
    return [
        {
            "threshold": threshold,
            "facts_before": before,
            "facts_after": after,
            "merged": merged,
            "recall@3_before": r3_before,
            "recall@3_after": r3_after,
            "recall_preserved": r3_after >= r3_before,
        }
    ]


def main(args: argparse.Namespace) -> int:
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    cfg = get_config()
    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    print(f"dataset: {len(data['facts'])} facts, {len(data['queries'])} paraphrased queries\n")
    results = []
    for spec in backends:
        try:
            results.append(evaluate(spec, data, cfg))
        except Exception as exc:
            print(f"[skipped {spec}] {exc}")
    print()
    if results:
        print_rows(results, BACKEND_COLS)
    else:
        print("no backends ran")
    _print_ci(results)
    _print_pairwise(results)
    if args.stm:
        stm_rows = evaluate_stm(data, cfg)
        if stm_rows:
            print("\nSTM lever (stm_recall_weight) — recall of fresh STM gold vs older LTM competitors:\n")
            print_rows(stm_rows, STM_COLS)
    if args.antipatterns:
        ap_rows = evaluate_antipatterns(data, cfg)
        if ap_rows:
            print("\nAnti-pattern union — recall of global anti-patterns from a different project:\n")
            print_rows(ap_rows, ANTIPATTERN_COLS)
    if args.integrate:
        int_rows = evaluate_integrate(data, cfg)
        if int_rows:
            print("\nIntegrate (gist chunking) — cluster collapses to one survivor, recall preserved:\n")
            print_rows(int_rows, INTEGRATE_COLS)
    if args.confidence or args.aged:
        distractors = load_distractors(args, cfg, data["facts"])
        if distractors is None:
            return 1
        if args.confidence:
            run_confidence(data, cfg, backends, distractors, args.ok_precision)
        if args.aged:
            run_aged(data, cfg, backends, distractors)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="compare embedding backends")
    add_eval_arguments(parser)
    sys.exit(main(parser.parse_args()))
