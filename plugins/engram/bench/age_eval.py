"""Age-aware ranking benchmark (``engram eval --aged``).

DESIGN.md's contract is that soft recency decay only *orders* non-conflicting facts —
conflicts are removed by supersession — so a relevant fact must not lose its rank merely for
being old. This measures that. Each dataset fact is stamped old (90-240 days) or new (0-14
days) by a seeded coin; queries are split by whether their gold facts are all old or all new;
and both production rankers are scored at their shipped weights and at weaker recency
settings, each against the same ranker with recency switched off — the *age-blind* ranking
(on one store, recency-off is exactly "age carries no weight"; a separate all-stamped-now
store is not neutral for fusion, whose recency channel then ranks by insertion order):

* ``search`` — the per-prompt injection hook (priority = sim·w_sim + decay·w_recency + freq·w_freq);
* ``search_fused`` — the on-demand ``recall`` tool (weighted rank fusion, recency channel).

McNemar exact tests compare old-gold hit@3 with the age-blind ranking (how much relevance age
overrides) and new-gold hit@3 with the shipped weights (what weakening recency costs recent
facts). The dataset cannot reward recency (nothing in it is "newer and therefore truer"), so
it answers a narrow question: at what weight does age stop overriding relevance, and what
does that cost recent facts?
"""

from __future__ import annotations

import random
import shutil
import tempfile
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest import mock

from bench.backends import make_embedder, parse_spec
from bench.report import print_rows
from bench.retrieval import RankFn, fused_ranker, score_queries, search_ranker
from bench.stats import mcnemar_exact
from bench.stores import build_store
from core.domain import fusion

DAY = 86400.0
OLD_DAYS = (90.0, 240.0)
NEW_DAYS = (0.0, 14.0)

Variant = tuple[str, dict]  # (label, overrides): Config fields (hook) or fusion channel weights (recall tool)

# Alternatives to the shipped weights. The shipped row is built from the live values, so its label
# can't go stale when a default changes, and an alternative equal to the shipped value is dropped.
_HOOK_ALTERNATIVES: list[Variant] = [
    ("half-life 180d", {"half_life_days": 180.0}),
    ("w_recency 0.3", {"w_recency": 0.3}),
    ("w_recency 0.1", {"w_recency": 0.1}),
    ("w_recency 0.05", {"w_recency": 0.05}),
    ("w_recency 0.02", {"w_recency": 0.02}),
]
_FUSED_ALTERNATIVES: list[Variant] = [("recency 0.2", {"recency": 0.2})]


def _variants(label: str, shipped: dict, alternatives: list[Variant], blind: Variant) -> list[Variant]:
    """Shipped first (no overrides), then the alternatives that differ from it, then age-blind last."""
    differing = [(name, o) for name, o in alternatives if any(shipped[key] != value for key, value in o.items())]
    return [(label, {}), *differing, blind]


def hook_variants(cfg) -> list[Variant]:
    shipped = {"w_recency": cfg.w_recency, "half_life_days": cfg.half_life_days}
    label = f"shipped (w_recency {cfg.w_recency:g}, half-life {cfg.half_life_days:g}d)"
    return _variants(label, shipped, _HOOK_ALTERNATIVES, ("age-blind (w_recency 0)", {"w_recency": 0.0}))


def fused_variants() -> list[Variant]:
    recency = fusion.DEFAULT_WEIGHTS["recency"]
    label = f"shipped (recency {recency:g})"
    return _variants(label, {"recency": recency}, _FUSED_ALTERNATIVES, ("age-blind (recency 0)", {"recency": 0.0}))


AGED_COLS = [
    "path",
    "variant",
    "old R@1",
    "old R@3",
    "old MRR",
    "old dR@3 vs age-blind",
    "new R@3",
    "new dR@3 vs shipped",
]


def stamp_ages(facts: list[str], now: float, seed: int = 0) -> tuple[list[tuple[str, float]], set[int]]:
    """Each fact stamped old or new by a seeded fair coin; returns the records and old indices."""
    rng = random.Random(seed)
    records, old = [], set()
    for index, text in enumerate(facts):
        is_old = rng.random() < 0.5
        lo, hi = OLD_DAYS if is_old else NEW_DAYS
        records.append((text, now - rng.uniform(lo, hi) * DAY))
        if is_old:
            old.add(index)
    return records, old


def split_by_age(queries: list[dict], old: set[int]) -> tuple[list[dict], list[dict]]:
    """Queries whose gold facts are all old / all new; mixed-age queries belong to neither."""
    old_q = [q for q in queries if q["relevant"] and all(i in old for i in q["relevant"])]
    new_q = [q for q in queries if q["relevant"] and not any(i in old for i in q["relevant"])]
    return old_q, new_q


def _hits(queries: list[dict], facts: list[str], rank_fn: RankFn) -> tuple[float, float, float, list[bool]]:
    r1, r3, mrr, _ms, per_query = score_queries(queries, facts, rank_fn)
    return r1, r3, mrr, [p["hit3"] for p in per_query]


def _paired_delta(base: list[bool], other: list[bool]) -> str:
    if not base:
        return "n/a"
    only_base = sum(1 for b, o in zip(base, other) if b and not o)
    only_other = sum(1 for b, o in zip(base, other) if o and not b)
    p = mcnemar_exact(only_base, only_other)
    mark = "*" if p < 0.05 else ""
    return f"{(only_other - only_base) / len(base):+.3f} (p={p:.3f}){mark}"


def _measure(
    path: str,
    variants: list[Variant],
    rank_for: Callable[[dict], RankFn],
    patch_fusion: bool,
    facts: list[str],
    old_q: list[dict],
    new_q: list[dict],
) -> list[dict]:
    """Score every variant on old- and new-gold queries; paired deltas against the age-blind
    (last) and shipped (first) variants.

    ``patch_fusion`` scopes a variant's overrides onto the fusion channel weights (the recall
    tool has no config knob for them); otherwise overrides are Config fields.
    """
    scored = []
    for label, overrides in variants:
        weights = mock.patch.dict(fusion.DEFAULT_WEIGHTS, overrides) if patch_fusion else nullcontext()
        with weights:
            rank = rank_for(overrides)
            r1, r3, mrr, old_hits = _hits(old_q, facts, rank)
            _, new_r3, _, new_hits = _hits(new_q, facts, rank)
        scored.append((label, overrides, r1, r3, mrr, old_hits, new_r3, new_hits))
    blind_old, shipped_new = scored[-1][5], scored[0][7]
    return [
        {
            "path": path,
            "variant": label,
            "old R@1": r1,
            "old R@3": r3,
            "old MRR": mrr,
            "old dR@3 vs age-blind": "—" if index == len(scored) - 1 else _paired_delta(blind_old, old_hits),
            "new R@3": new_r3,
            "new dR@3 vs shipped": "—" if index == 0 else _paired_delta(shipped_new, new_hits),
        }
        for index, (label, _o, r1, r3, mrr, old_hits, new_r3, new_hits) in enumerate(scored)
    ]


def evaluate_aged(spec: str, data: dict, cfg, distractors: list[tuple[str, float]], seed: int = 0) -> dict:
    name, model, truncate_dim, float_mode = parse_spec(spec)
    if float_mode:
        raise ValueError("+float ranks outside the store; the age benchmark needs the real ranking paths")
    embedder = make_embedder(name, model, truncate_dim, cfg)
    facts, queries = data["facts"], data["queries"]
    records, old = stamp_ages(facts, time.time(), seed)
    old_q, new_q = split_by_age(queries, old)
    root = Path(tempfile.mkdtemp(prefix="engram-bench-aged-"))
    try:
        store, project = build_store(embedder, cfg, records + list(distractors), root, key="aged")
        try:
            rows = _measure(
                "hook (search)",
                hook_variants(cfg),
                lambda o: search_ranker(store, embedder, project, replace(cfg, **o)),
                False,
                facts,
                old_q,
                new_q,
            )
            rows += _measure(
                "recall tool (search_fused)",
                fused_variants(),
                lambda _o: fused_ranker(store, embedder, project, cfg),
                True,
                facts,
                old_q,
                new_q,
            )
        finally:
            store.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return {"backend": spec, "old_n": len(old_q), "new_n": len(new_q), "rows": rows}


def run_aged(data: dict, cfg, backends: list[str], distractors: list[tuple[str, float]]) -> None:
    for spec in backends:
        try:
            result = evaluate_aged(spec, data, cfg, distractors)
        except Exception as exc:
            print(f"[aged skipped {spec}] {exc}")
            continue
        print(
            f"\nAge-aware ranking — {spec}, {len(distractors)} distractors: {result['old_n']} old-gold / "
            f"{result['new_n']} new-gold queries (old {OLD_DAYS[0]:.0f}-{OLD_DAYS[1]:.0f}d, new "
            f"{NEW_DAYS[0]:.0f}-{NEW_DAYS[1]:.0f}d)\n"
        )
        print_rows(result["rows"], AGED_COLS)
        print(
            "\n  d = paired McNemar exact on hit@3; * = p<0.05. Age-blind = the same ranker with recency off"
            " (old-gold: how much relevance age overrides; new-gold: what weakening recency costs recent facts)."
        )
