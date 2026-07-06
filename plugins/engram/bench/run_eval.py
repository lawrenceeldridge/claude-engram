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
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import random
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

from core import service  # noqa: E402
from core.config import get_config  # noqa: E402
from core.consolidation.integrate import integrate  # noqa: E402
from core.domain.quantize import cosine  # noqa: E402
from core.ports.distill import DistilledFact  # noqa: E402
from core.ports.embedding import EmbeddingGateway, HashEmbedding  # noqa: E402
from core.project import global_project  # noqa: E402
from core.recall import search  # noqa: E402
from core.store import Store  # noqa: E402

DATASET = Path(__file__).resolve().parent / "dataset.json"


def parse_spec(spec: str) -> tuple[str, str | None, int, bool]:
    """``name[@model][%truncate_dim][+float]`` — %N truncates Matryoshka vectors to N dims."""
    float_mode = spec.endswith("+float")
    core = spec[: -len("+float")] if float_mode else spec
    core, _, trunc = core.partition("%")
    name, _, model = core.partition("@")
    return name, (model or None), int(trunc) if trunc else 0, float_mode


def make_embedder(name: str, model: str | None, truncate_dim: int, cfg) -> EmbeddingGateway:
    if name == "hash":
        return HashEmbedding(dim=cfg.dim)
    if name == "fastembed":
        from core.adapters.fastembed_gw import FastEmbedGateway

        return FastEmbedGateway(model, truncate_dim=truncate_dim)
    raise ValueError(f"unknown backend {name!r}")


def _score_queries(queries: list[dict], facts: list[str], rank_fn) -> tuple[float, float, float, float, list[dict]]:
    """Aggregate metrics plus per-query records, so backends evaluated on the same
    queries can be compared with paired tests (`_print_pairwise`) rather than only
    independent intervals."""
    hit1 = hit3 = mrr = 0.0
    latencies = []
    per_query: list[dict] = []
    for item in queries:
        gold = {facts[i] for i in item["relevant"]}
        start = time.perf_counter()
        ranked = rank_fn(item["q"])
        latencies.append((time.perf_counter() - start) * 1000)
        q_hit1 = bool(ranked[:1] and ranked[0] in gold)
        q_hit3 = any(text in gold for text in ranked[:3])
        q_rr = 0.0
        for rank, text in enumerate(ranked[:10], start=1):
            if text in gold:
                q_rr = 1.0 / rank
                break
        hit1 += q_hit1
        hit3 += q_hit3
        mrr += q_rr
        per_query.append({"q": item["q"], "hit1": q_hit1, "hit3": q_hit3, "rr": q_rr})
    n = len(queries)
    return hit1 / n, hit3 / n, mrr / n, statistics.mean(latencies), per_query


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

        def rank_fn(query: str) -> list[str]:
            return [row["text"] for _score, row in search(store, embedder, project, query, cfg, k=10, min_sim=-1.0)]

    r1, r3, mrr, query_ms, per_query = _score_queries(queries, facts, rank_fn)
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


def wilson(k: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% interval for a proportion ``k/n``. Honest at small n and near 0/1.

    ``k`` is passed as a float (rate*n rounded) since the caller carries rates, not counts.
    Returns ``(lo, hi)``; a zero-width span for ``n == 0``.
    """
    if n <= 0:
        return 0.0, 0.0
    k = max(0.0, min(float(n), round(k)))
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def mcnemar_exact(b: int, c: int) -> float:
    """Exact two-sided McNemar p-value from discordant pair counts.

    ``b`` = queries backend A got right and B wrong; ``c`` = the reverse.
    Concordant pairs carry no information, so this is an exact sign test on
    the discordant pairs — honest at the small counts this bench produces.
    Returns 1.0 when there are no discordant pairs.
    """
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2.0**n
    return min(1.0, 2.0 * tail)


def bootstrap_ci(deltas: list[float], iters: int = 10_000, seed: int = 0) -> tuple[float, float]:
    """Seeded percentile-bootstrap 95% CI for the mean of per-query deltas.

    The seed is fixed so every run of the bench prints the same interval —
    reproducibility over randomness, as with everything else in this harness.
    """
    if not deltas:
        return 0.0, 0.0
    rng = random.Random(seed)
    n = len(deltas)
    means = sorted(sum(deltas[rng.randrange(n)] for _ in range(n)) / n for _ in range(iters))
    return means[int(0.025 * iters)], means[int(0.975 * iters)]


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.3f}" if value < 100 else f"{value:.1f}"
    return str(value)


def _print_table(results: list[dict]) -> None:
    if not results:
        print("no backends ran")
        return
    cols = ["backend", "dim", "recall@1", "recall@3", "mrr@10", "embed_ms/fact", "query_ms", "bytes/fact"]
    widths = {c: max(len(c), *(len(_fmt(r[c])) for r in results)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in results:
        print("  ".join(_fmt(r[c]).ljust(widths[c]) for c in cols))


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

        def rank_fn(query: str, _cfg=cfg, _store=store, _emb=embedder, _proj=project) -> list[str]:
            return [row["text"] for _score, row in search(_store, _emb, _proj, query, _cfg, k=10, min_sim=-1.0)]

        r1, r3, mrr, _, _ = _score_queries(queries, facts, rank_fn)
        store.close()
        rows_out.append({"stm_recall_weight": weight, "stm_recall@1": r1, "stm_recall@3": r3, "stm_mrr@10": mrr})
    return rows_out


def _print_stm_table(rows: list[dict]) -> None:
    cols = ["stm_recall_weight", "stm_recall@1", "stm_recall@3", "stm_mrr@10"]
    widths = {c: max(len(c), *(len(_fmt(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(_fmt(r[c]).ljust(widths[c]) for c in cols))


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

    def rank_fn(query: str, _cfg=cfg, _store=store, _emb=embedder, _proj=project) -> list[str]:
        return [row["text"] for _score, row in search(_store, _emb, _proj, query, _cfg, k=10, min_sim=-1.0)]

    r1, r3, mrr, _, _ = _score_queries(queries, antipatterns, rank_fn)
    store.close()
    return [{"scope": "global", "recall@1": r1, "recall@3": r3, "mrr@10": mrr}]


def _print_antipattern_table(rows: list[dict]) -> None:
    cols = ["scope", "recall@1", "recall@3", "mrr@10"]
    widths = {c: max(len(c), *(len(_fmt(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(_fmt(r[c]).ljust(widths[c]) for c in cols))


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

    def rank_fn(query: str) -> list[str]:
        return [row["text"] for _s, row in search(store, embedder, project, query, cfg, k=10, min_sim=-1.0)]

    before = len(store.active_rows_for_project(project["key"]))
    r1_before, r3_before, _, _, _ = _score_queries(queries, facts, rank_fn)
    merged = integrate(store, cfg, project, embedder=embedder)
    after = len(store.active_rows_for_project(project["key"]))
    r1_after, r3_after, _, _, _ = _score_queries(queries, facts, rank_fn)
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


def _print_integrate_table(rows: list[dict]) -> None:
    cols = [
        "threshold",
        "facts_before",
        "facts_after",
        "merged",
        "recall@3_before",
        "recall@3_after",
        "recall_preserved",
    ]
    widths = {c: max(len(c), *(len(_fmt(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(_fmt(r[c]).ljust(widths[c]) for c in cols))


def main(backends: list[str], stm: bool = False, antipatterns: bool = False, integrate_stage: bool = False) -> int:
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    cfg = get_config()
    print(f"dataset: {len(data['facts'])} facts, {len(data['queries'])} paraphrased queries\n")
    results = []
    for spec in backends:
        try:
            results.append(evaluate(spec, data, cfg))
        except Exception as exc:
            print(f"[skipped {spec}] {exc}")
    print()
    _print_table(results)
    _print_ci(results)
    _print_pairwise(results)
    if stm:
        stm_rows = evaluate_stm(data, cfg)
        if stm_rows:
            print("\nSTM lever (stm_recall_weight) — recall of fresh STM gold vs older LTM competitors:\n")
            _print_stm_table(stm_rows)
    if antipatterns:
        ap_rows = evaluate_antipatterns(data, cfg)
        if ap_rows:
            print("\nAnti-pattern union — recall of global anti-patterns from a different project:\n")
            _print_antipattern_table(ap_rows)
    if integrate_stage:
        int_rows = evaluate_integrate(data, cfg)
        if int_rows:
            print("\nIntegrate (gist chunking) — cluster collapses to one survivor, recall preserved:\n")
            _print_integrate_table(int_rows)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="compare embedding backends")
    parser.add_argument("--backends", default="hash", help="comma-separated specs: name[@model][+float]")
    parser.add_argument("--stm", action="store_true", help="also run the STM-tier lever scenario")
    parser.add_argument(
        "--antipatterns", action="store_true", help="also run the global anti-pattern surfacing scenario"
    )
    parser.add_argument("--integrate", action="store_true", help="also run the integrate (gist-chunking) scenario")
    args = parser.parse_args()
    sys.exit(
        main(
            [b.strip() for b in args.backends.split(",") if b.strip()],
            stm=args.stm,
            antipatterns=args.antipatterns,
            integrate_stage=args.integrate,
        )
    )
