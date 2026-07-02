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
    python3 bin/ltm eval --backends hash,fastembed
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.embedding import EmbeddingGateway, HashEmbedding  # noqa: E402
from core.quantize import cosine  # noqa: E402
from core.recall import search  # noqa: E402
from core.store import Store  # noqa: E402
from core import service  # noqa: E402

DATASET = Path(__file__).resolve().parent / "dataset.json"


def parse_spec(spec: str) -> tuple[str, str | None, bool]:
    float_mode = spec.endswith("+float")
    core = spec[: -len("+float")] if float_mode else spec
    name, _, model = core.partition("@")
    return name, (model or None), float_mode


def make_embedder(name: str, model: str | None, cfg) -> EmbeddingGateway:
    if name == "hash":
        return HashEmbedding(dim=cfg.dim)
    if name == "fastembed":
        from core.adapters.fastembed_gw import FastEmbedGateway

        return FastEmbedGateway(model)
    raise ValueError(f"unknown backend {name!r}")


def _score_queries(queries: list[dict], facts: list[str], rank_fn) -> tuple[float, float, float, float]:
    hit1 = hit3 = mrr = 0.0
    latencies = []
    for item in queries:
        gold = {facts[i] for i in item["relevant"]}
        start = time.perf_counter()
        ranked = rank_fn(item["q"])
        latencies.append((time.perf_counter() - start) * 1000)
        if ranked[:1] and ranked[0] in gold:
            hit1 += 1
        if any(text in gold for text in ranked[:3]):
            hit3 += 1
        for rank, text in enumerate(ranked[:10], start=1):
            if text in gold:
                mrr += 1.0 / rank
                break
    n = len(queries)
    return hit1 / n, hit3 / n, mrr / n, statistics.mean(latencies)


def evaluate(spec: str, data: dict, base_cfg) -> dict:
    name, model, float_mode = parse_spec(spec)
    cfg = replace(base_cfg, supersede_threshold=1.0, top_k=10, min_sim=-1.0)
    embedder = make_embedder(name, model, cfg)
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
        tmp = tempfile.mkdtemp(prefix="ltm-bench-")
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

    r1, r3, mrr, query_ms = _score_queries(queries, facts, rank_fn)
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
    }


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


def main(backends: list[str]) -> int:
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
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="compare embedding backends")
    parser.add_argument("--backends", default="hash", help="comma-separated specs: name[@model][+float]")
    args = parser.parse_args()
    sys.exit(main([b.strip() for b in args.backends.split(",") if b.strip()]))
