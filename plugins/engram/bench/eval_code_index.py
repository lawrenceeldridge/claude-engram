#!/usr/bin/env python3
"""6b scoping experiment: does a code-specialised embedding model beat the general
default on the *index* surface (search_code-style retrieval)?

Builds a fresh temp index of this plugin's own tree per backend, then runs a
small labelled set of natural-language code queries whose gold answer is a known
symbol anchor. Decision rule (tracker 6.2): per-surface model config is only
justified if the code model clears the general default by >= 0.10 Recall@1;
otherwise 6b closes with "not worth the complexity".

Run (from plugins/engram/):
    python3 bench/eval_code_index.py
    python3 bench/eval_code_index.py --backends "fastembed@BAAI/bge-base-en-v1.5,fastembed@jinaai/jina-embeddings-v2-base-code"
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from run_eval import make_embedder, parse_spec, wilson  # noqa: E402

from core.config import get_config  # noqa: E402
from core.index.index_recall import search_index  # noqa: E402
from core.index.indexer import index_project  # noqa: E402
from core.store import Store  # noqa: E402

# Natural-language query -> gold symbol anchor (dotted qualname suffix match).
QUERIES: list[tuple[str, str]] = [
    ("function that renders the injected memory block one line per fact", "render_block"),
    ("where is the token savings summary aggregated for the stats command", "usage_summary"),
    ("method returning a project's active fact rows", "Store.active_rows_for_project"),
    ("exact two-sided test on discordant query pairs", "mcnemar_exact"),
    ("quantise a float vector to signed bytes", "quantize_int8"),
    ("restore floats from an int8 blob and its scale", "dequantize_int8"),
    ("cosine similarity between two vectors", "cosine"),
    ("walk up directories to identify the project by marker files", "resolve_project"),
    ("gateway class wrapping the ONNX embedding models", "FastEmbedGateway"),
    ("keep only the leading dimensions of a vector and renormalise", "truncate_renorm"),
    ("record one usage ledger row with cost or saving bytes", "Store.record_usage"),
    ("append a recall event to the telemetry log", "Store.log_recall"),
    ("store distilled facts with reinforcement and supersession", "add_records"),
    ("re-distil parked degraded deltas from the durable queue", "rescue"),
    ("pick the embedding implementation from config with fallback", "get_embedder"),
    ("zero-dependency lexical embedding stub", "HashEmbedding"),
    ("find facts an incoming vector should replace", "_find_superseded"),
    ("hybrid keyword and semantic search over indexed chunks", "search_index"),
    ("fetch one indexed chunk body by its anchor", "get_chunk"),
    ("index or refresh all code and docs under a root", "index_project"),
    ("split source code into symbol units for indexing", "_code_units"),
    ("parse a backend spec string into name model and flags", "parse_spec"),
    ("score interval for a proportion at small sample sizes", "wilson"),
    ("signed-rank exact p value for paired differences", "wilcoxon_exact"),
    ("detect search sweeps in a parsed transcript", "find_sweeps"),
    ("flatten transcript lines into tool call events", "parse_transcript"),
    ("compute the composite token cost from a usage record", "composite_tokens"),
    ("record co-occurrence and shared entity edges between facts", "_record_edges"),
    ("one compact outline line per index hit", "_render_index_block"),
    ("check an off-arm transcript is free of plugin activity", "transcript_is_clean"),
]

DEFAULT_BACKENDS = "fastembed@BAAI/bge-base-en-v1.5,fastembed@jinaai/jina-embeddings-v2-base-code"


def _matches(anchor: str, gold: str) -> bool:
    a, g = anchor.lower(), gold.lower()
    return a == g or a.endswith("." + g.rsplit(".", 1)[-1]) and g.rsplit(".", 1)[-1] in a


def evaluate_backend(spec: str, base_cfg) -> dict:
    name, model, truncate_dim, _float = parse_spec(spec)
    cfg = replace(base_cfg, index_top_k=10, index_min_sim=-1.0)
    embedder = make_embedder(name, model, truncate_dim, cfg)
    tmp = tempfile.mkdtemp(prefix="engram-codeidx-")
    store = Store(Path(tmp) / "eval.db")
    project = {"key": f"codeidx-{abs(hash(spec)) % 99999}", "path": str(ROOT), "label": "codeidx"}
    start = time.perf_counter()
    index_project(store, embedder, cfg, project, ROOT, max_files=None)
    index_ms = (time.perf_counter() - start) * 1000

    hit1 = hit3 = 0
    per_query = []
    for q, gold in QUERIES:
        res = search_index(store, embedder, cfg, project, q, k=10, max_chars=100_000, kind="code_symbol")
        anchors = [r.get("anchor") or "" for r in res.get("results") or []]
        h1 = bool(anchors[:1]) and _matches(anchors[0], gold)
        h3 = any(_matches(a, gold) for a in anchors[:3])
        hit1 += h1
        hit3 += h3
        per_query.append({"q": q, "hit1": h1, "hit3": h3})
    store.close()
    n = len(QUERIES)
    return {
        "backend": spec,
        "recall@1": hit1 / n,
        "recall@3": hit3 / n,
        "index_ms": index_ms,
        "n": n,
        "per_query": per_query,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="code-index model scoping (6b)")
    parser.add_argument("--backends", default=DEFAULT_BACKENDS)
    args = parser.parse_args()
    cfg = get_config()
    results = []
    for spec in [b.strip() for b in args.backends.split(",") if b.strip()]:
        try:
            results.append(evaluate_backend(spec, cfg))
        except Exception as exc:
            print(f"[skipped {spec}] {exc}")
    for r in results:
        lo, hi = wilson(r["recall@1"] * r["n"], r["n"])
        print(
            f"{r['backend']}: recall@1 {r['recall@1']:.3f} [{lo:.3f}, {hi:.3f}]  "
            f"recall@3 {r['recall@3']:.3f}  (n={r['n']}, index {r['index_ms']:.0f}ms)"
        )
    if len(results) == 2:
        a, b = results
        d1 = b["recall@1"] - a["recall@1"]
        disc_a = sum(1 for x, y in zip(a["per_query"], b["per_query"]) if x["hit1"] and not y["hit1"])
        disc_b = sum(1 for x, y in zip(a["per_query"], b["per_query"]) if y["hit1"] and not x["hit1"])
        from run_eval import mcnemar_exact

        print(
            f"\ndelta recall@1 (code model - general): {d1:+.3f} "
            f"(discordant {disc_a}/{disc_b}, McNemar p={mcnemar_exact(disc_a, disc_b):.3f})"
        )
        print("decision rule: per-surface model config justified only if delta >= +0.10")
    return 0


if __name__ == "__main__":
    sys.exit(main())
