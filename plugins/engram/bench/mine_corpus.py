#!/usr/bin/env python3
"""Mine benchmark-corpus candidates from the live memory store.

Dev tool for growing ``bench/dataset.json`` (run manually, not wired into the
CLI). Reads *this project's* active facts through the Store repository, then:

1. drops facts that could leak benchmark labels into the corpus
   (contamination — a fact that mentions recall@1 numbers would let a model
   score by memorising the metric line, not by retrieval);
2. flags facts that must not ship in a public dataset (privacy — non-repo
   paths, emails, credential-shaped strings) for the mandatory human pass;
3. deduplicates near-identical facts on their stored vectors;
4. emits ``bench/candidates.json`` grouped into similarity clusters, ready
   for the human review gate and paraphrase-query authoring.

Nothing is written to the store; this is a read-only pass. Candidates are
NOT the dataset — every fact still goes through a human privacy/quality
review before any of it reaches ``dataset.json``.

Run:
    python3 bench/mine_corpus.py                       # current project
    python3 bench/mine_corpus.py --min-len 50 --max 400
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.domain.quantize import cosine, dequantize_int8  # noqa: E402
from core.project import resolve_project  # noqa: E402
from core.store import Store  # noqa: E402

OUT = Path(__file__).resolve().parent / "candidates.json"

# Label-leakage guard: facts *about the benchmark itself* would let a backend
# score by memorising metric lines rather than retrieving. Case-insensitive.
CONTAMINATION = (
    "recall@",
    "mrr",
    "benchmark",
    "whitepaper",
    "dataset.json",
    "wilson",
    "engram eval",
    "run_eval",
    "bench/",
    "paraphrase",
    "confidence interval",
    # Measurement-machinery meta (the bench-rigour work): facts *about* the
    # harness, corpus, or statistics would leak design labels into the corpus.
    # Deliberately over-broad — e.g. "replay" also drops legit consolidation
    # facts; losing a few real facts beats contaminating the public dataset.
    "counterfactual",
    "wilcoxon",
    "mcnemar",
    "bootstrap",
    "bake-off",
    "bakeoff",
    "matryoshka",
    "hard negative",
    "hard-negative",
    "multi-relevant",
    "corpus",
    "paired",
    "a/b",
    "replay",
    "arctic",
    "mxbai",
    "nomic",
)

# Privacy flaggers — matches route the fact to the manual pile, never auto-accept.
RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
RE_SECRET = re.compile(r"(api[_-]?key|secret|token|password|bearer|aws_|sk-[A-Za-z0-9]{8,})", re.IGNORECASE)
RE_ABS_PATH = re.compile(r"/(?:Users|home)/[\w.-]+/[^\s'\"]*")

DEDUPE_SIM = 0.90  # near-identical wording
CLUSTER_SIM = 0.60  # same-topic grouping for reviewer convenience


def contamination_hit(text: str) -> str | None:
    low = text.lower()
    for marker in CONTAMINATION:
        if marker in low:
            return marker
    return None


def privacy_flags(text: str, repo_path: str) -> list[str]:
    flags = []
    if RE_EMAIL.search(text):
        flags.append("email")
    if RE_SECRET.search(text):
        flags.append("credential-shaped")
    for path in RE_ABS_PATH.findall(text):
        if not path.startswith(repo_path):
            flags.append(f"non-repo path: {path[:60]}")
            break
    return flags


def mine(store: Store, project_key: str, repo_path: str, min_len: int, max_facts: int) -> dict:
    rows = [r for r in store.active_rows_for_project(project_key) if r["kind"] == "fact"]
    # Newest first: recent facts describe the codebase as it is now.
    rows.sort(key=lambda r: r["last_seen"] or r["created_at"], reverse=True)

    dropped = {"short": 0, "contaminated": 0, "duplicate": 0}
    kept: list[dict] = []
    kept_vecs: list[list[float]] = []
    for row in rows:
        if len(kept) >= max_facts:
            break
        text = (row["text"] or "").strip()
        if len(text) < min_len:
            dropped["short"] += 1
            continue
        marker = contamination_hit(text)
        if marker:
            dropped["contaminated"] += 1
            continue
        vec = dequantize_int8(row["vec_int8"], row["scale"]) if row["vec_int8"] else None
        if vec is not None and any(cosine(vec, v) >= DEDUPE_SIM for v in kept_vecs):
            dropped["duplicate"] += 1
            continue
        kept.append(
            {
                "text": text,
                "flags": privacy_flags(text, repo_path),
                "created_at": row["created_at"],
                "frequency": row["frequency"],
                "cluster": None,
            }
        )
        if vec is not None:
            kept_vecs.append(vec)

    # Greedy leader clustering so the reviewer sees same-topic facts together.
    leaders: list[tuple[int, list[float]]] = []
    for i, vec in enumerate(kept_vecs):
        for cluster_id, leader_vec in leaders:
            if cosine(vec, leader_vec) >= CLUSTER_SIM:
                kept[i]["cluster"] = cluster_id
                break
        else:
            kept[i]["cluster"] = len(leaders)
            leaders.append((len(leaders), vec))

    flagged = sum(1 for c in kept if c["flags"])
    return {
        "project_key": project_key,
        "candidates": kept,
        "clusters": len(leaders),
        "flagged_for_review": flagged,
        "dropped": dropped,
        "note": (
            "HUMAN GATE: review every candidate (especially flagged ones) for privacy "
            "and quality before authoring queries — dataset.json ships in a public repo."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="mine dataset candidates from the live store")
    parser.add_argument("--min-len", type=int, default=40, help="drop facts shorter than this")
    parser.add_argument("--max", type=int, default=400, help="max candidates to emit")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    cfg = get_config()
    project = resolve_project(os.getcwd(), cfg.markers)
    store = Store(cfg.db_path)
    try:
        result = mine(store, project["key"], project["path"], args.min_len, args.max)
    finally:
        store.close()

    args.out.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    d = result["dropped"]
    print(f"project     : {project['label']} [{project['key']}]")
    print(
        f"candidates  : {len(result['candidates'])} kept in {result['clusters']} clusters "
        f"({result['flagged_for_review']} flagged for review)"
    )
    print(f"dropped     : {d['short']} short, {d['contaminated']} contaminated, {d['duplicate']} duplicate")
    print(f"wrote       : {args.out}")
    print("next        : HUMAN review gate, then paraphrase-query authoring (tracker 2.3)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
