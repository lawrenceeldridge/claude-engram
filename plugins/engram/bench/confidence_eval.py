"""Calibration benchmark for the recall tool's verdict (``engram eval --confidence``).

The verdict is only useful if ``ok`` means "the returned facts contain the answer", so
that is measured directly. Every query runs through the real on-demand recall path
(``search_fused_with_stats`` at ``activated_k``) and is labelled **positive** when it is
answerable *and* a gold fact is among the returned hits; unanswerable queries — and
answerable ones whose answer was not returned — are negatives. Each candidate score is
judged on:

* **discrimination** — AUROC (rank-based, so invariant to rescaling), with a paired
  seeded-bootstrap delta against the shipped formula;
* **calibration** — Brier and ECE of 2-fold cross-fitted Platt probabilities;
* **the gate** — precision/recall of ``ok`` at calibrated ``p >= ok_precision`` (the verdict
  as a calibrated score would ship), and for the shipped formula also at the configured
  ``recall_min_confidence`` (the verdict as it ships today).

Candidates score a ``FusedResult`` — the exact object production recall computes — and
``current`` *is* production's ``recall_confidence``, so what is measured is what ships.
"""

from __future__ import annotations

import random
import shutil
import statistics
import tempfile
from collections.abc import Callable
from pathlib import Path

from bench.backends import make_embedder, parse_spec
from bench.report import print_rows
from bench.stats import auroc, bootstrap_stat_ci, brier, ece, platt_apply, platt_fit, wilson
from bench.stores import build_store
from core.domain.confidence import PoolStats
from core.ports.embedding import EmbeddingGateway
from core.recall import FusedResult, best_match, recall_confidence, search_fused_with_stats
from core.store import Store

NO_RECALL = float("-inf")  # an empty result can never be `ok`; ranks below every real score

Candidate = Callable[[str, FusedResult], float]


def _z(value: float, pool: PoolStats) -> float:
    return (value - pool.mean) / pool.std if pool.std > 0 else 0.0


def _top1(result: FusedResult) -> float:
    return best_match(result.hits)[1]


CANDIDATES: dict[str, Candidate] = {
    # The shipped formula: gap x strength x identity over the returned sims.
    "current": lambda query, result: recall_confidence(query, result)["confidence"],
    # Absolute strength of the best match, no context.
    "top1": lambda _query, result: _top1(result),
    # Best match against the whole scanned pool (plan candidate A).
    "pool_z": lambda _query, result: _z(_top1(result), result.pool),
    # Mean returned similarity against the pool — evidence mass (plan candidate C).
    "topk_z": lambda _query, result: _z(statistics.fmean(sim for _s, sim, _r in result.hits), result.pool),
}


def candidate_scores(query: str, result: FusedResult) -> dict[str, float]:
    """Every candidate's raw score for one recall (``NO_RECALL`` when nothing came back)."""
    if not result.hits:
        return {name: NO_RECALL for name in CANDIDATES}
    return {name: fn(query, result) for name, fn in CANDIDATES.items()}


def dataset_records(
    facts: list[str], distractors: list[tuple[str, float]], seed: int = 0
) -> list[tuple[str, float | None]]:
    """Dataset facts stamped with ages drawn (seeded) from the distractors', then the distractors.

    Distractors keep their real timestamps, so fusion's recency channel neither favours nor
    buries the gold. Without distractors every fact is stamped now, as in the standard eval.
    """
    rng = random.Random(seed)
    ages = [ts for _text, ts in distractors]
    return [(text, rng.choice(ages) if ages else None) for text in facts] + list(distractors)


def observe(
    store: Store, embedder: EmbeddingGateway, project: dict, cfg, labelled: list[tuple[str, set[str]]]
) -> list[dict]:
    """Run each ``(query, gold_texts)`` through production recall; label and score it."""
    out = []
    for query, gold in labelled:
        result = search_fused_with_stats(store, embedder, project, query, cfg, k=cfg.activated_k)
        returned = {row["text"] for _s, _sim, row in result.hits}
        out.append({"q": query, "label": bool(gold & returned), "scores": candidate_scores(query, result)})
    return out


def cross_fit(scores: list[float], labels: list[bool], seed: int = 0) -> list[float]:
    """Out-of-fold Platt probabilities: fit on one seeded half, apply to the other, and swap.

    Rows with no recall are excluded from fitting and map to probability 0.
    """
    order = list(range(len(scores)))
    random.Random(seed).shuffle(order)
    folds = (order[0::2], order[1::2])
    probs = [0.0] * len(scores)
    for held_out, train in ((folds[0], folds[1]), (folds[1], folds[0])):
        fit = [i for i in train if scores[i] != NO_RECALL]
        params = platt_fit([scores[i] for i in fit], [labels[i] for i in fit])
        for i in held_out:
            probs[i] = platt_apply(scores[i], params)
    return probs


def gate(ok: list[bool], labels: list[bool]) -> dict:
    """Precision and recall of an `ok` verdict, each with a Wilson 95% interval."""
    tp = sum(1 for o, y in zip(ok, labels) if o and y)
    n_ok = sum(ok)
    positives = sum(labels)
    return {
        "ok_n": n_ok,
        "precision": tp / n_ok if n_ok else None,
        "precision_ci": wilson(tp, n_ok),
        "recall": tp / positives if positives else None,
        "recall_ci": wilson(tp, positives),
    }


def _ci(lo: float, hi: float) -> str:
    return f"[{lo:.3f}, {hi:.3f}]"


BOOTSTRAP_ITERS = 2000  # AUROC is O(n log n) per draw; 2k draws keep the run to seconds


def _resampled_auroc(scores: list[float], labels: list[bool]) -> Callable[[list[int]], float | None]:
    return lambda idx: auroc([scores[i] for i in idx], [labels[i] for i in idx])


def summarise(observations: list[dict], ok_precision: float, shipped_threshold: float) -> dict:
    """Per-candidate discrimination, calibration and gate metrics over labelled observations."""
    labels = [o["label"] for o in observations]
    n = len(labels)
    by_name = {name: [o["scores"][name] for o in observations] for name in CANDIDATES}
    current = by_name["current"]
    current_auroc = _resampled_auroc(current, labels)
    rows = []
    for name, scores in by_name.items():
        area = auroc(scores, labels)
        this_auroc = _resampled_auroc(scores, labels)
        lo, hi = bootstrap_stat_ci(n, this_auroc, BOOTSTRAP_ITERS)

        def delta(idx: list[int], this_auroc=this_auroc) -> float | None:
            a, b = this_auroc(idx), current_auroc(idx)
            return None if a is None or b is None else a - b

        if name == "current" or area is None:
            d_cell = "—"
        else:
            d_lo, d_hi = bootstrap_stat_ci(n, delta, BOOTSTRAP_ITERS)
            d_cell = f"{area - auroc(current, labels):+.3f} {_ci(d_lo, d_hi)}"
        probs = cross_fit(scores, labels)
        at_target = gate([p >= ok_precision for p in probs], labels)
        rows.append(
            {
                "candidate": name,
                "auroc": area,
                "auroc 95% CI": _ci(lo, hi),
                "dAUROC vs current": d_cell,
                "brier": brier(probs, labels),
                "ece": ece(probs, labels),
                "ok_n": at_target["ok_n"],
                "ok precision": _gate_cell(at_target["precision"], at_target["precision_ci"]),
                "ok recall": _gate_cell(at_target["recall"], at_target["recall_ci"]),
            }
        )
    shipped = gate([s >= shipped_threshold for s in current], labels)
    return {"n": n, "positives": sum(labels), "rows": rows, "shipped": shipped}


def _gate_cell(value: float | None, ci: tuple[float, float]) -> str:
    return "n/a" if value is None else f"{value:.3f} {_ci(*ci)}"


CONFIDENCE_COLS = [
    "candidate",
    "auroc",
    "auroc 95% CI",
    "dAUROC vs current",
    "brier",
    "ece",
    "ok_n",
    "ok precision",
    "ok recall",
]


def evaluate_confidence(spec: str, data: dict, cfg, distractors: list[tuple[str, float]], ok_precision: float) -> dict:
    name, model, truncate_dim, float_mode = parse_spec(spec)
    if float_mode:
        raise ValueError("+float ranks outside the store; the recall verdict needs the real path")
    embedder = make_embedder(name, model, truncate_dim, cfg)
    facts = data["facts"]
    labelled = [(q["q"], {facts[i] for i in q["relevant"]}) for q in data["queries"]]
    labelled += [(q, set()) for q in data["confidence_scenario"]["unanswerable"]]
    root = Path(tempfile.mkdtemp(prefix="engram-bench-conf-"))
    try:
        store, project = build_store(embedder, cfg, dataset_records(facts, distractors), root)
        try:
            observations = observe(store, embedder, project, cfg, labelled)
        finally:
            store.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    summary = summarise(observations, ok_precision, cfg.recall_min_confidence)
    summary.update(backend=spec, answerable=len(data["queries"]), unanswerable=len(labelled) - len(data["queries"]))
    return summary


def run_confidence(
    data: dict, cfg, backends: list[str], distractors: list[tuple[str, float]], ok_precision: float
) -> None:
    for spec in backends:
        try:
            summary = evaluate_confidence(spec, data, cfg, distractors, ok_precision)
        except Exception as exc:
            print(f"[confidence skipped {spec}] {exc}")
            continue
        print(
            f"\nRecall-verdict calibration — {spec}, k={cfg.activated_k}, {len(distractors)} distractors: "
            f"{summary['answerable']} answerable + {summary['unanswerable']} unanswerable queries, "
            f"{summary['positives']}/{summary['n']} positive (gold returned)\n"
        )
        print_rows(summary["rows"], CONFIDENCE_COLS)
        shipped = summary["shipped"]
        print(
            f"\n  shipped gate (current >= recall_min_confidence {cfg.recall_min_confidence}): ok_n {shipped['ok_n']}, "
            f"precision {_gate_cell(shipped['precision'], shipped['precision_ci'])}, "
            f"recall {_gate_cell(shipped['recall'], shipped['recall_ci'])}"
        )
        print(f"  ok precision / recall above are at calibrated p >= {ok_precision} (2-fold cross-fitted Platt)")
