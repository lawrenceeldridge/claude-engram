"""Calibration benchmark for the recall tool's verdict (``engram eval --confidence``).

The verdict is only useful if ``ok`` means "the returned facts contain the answer", so
that is measured directly. Every query runs through the real on-demand recall path
(``search_fused_with_stats`` at ``activated_k``) and is labelled **positive** when it is
answerable *and* a gold fact is among the returned hits; unanswerable queries — and
answerable ones whose answer was not returned — are negatives. Each candidate score is
judged on:

* **discrimination** — AUROC (rank-based, so invariant to rescaling), with a paired
  seeded-bootstrap delta against the shipped formula;
* **calibration** — Brier and ECE of Platt probabilities, plus each candidate's Platt ``(a, b)`` —
  the fit a shipped ``Calibration`` takes its constants from;
* **the gate** — precision/recall of ``ok`` at calibrated ``p >= ok_precision``, and for the
  shipped score also at the configured ``recall_min_confidence`` through production's own
  ``is_trusted`` rule (the verdict as it ships today).

**Tuned on dev, reported on test.** The queries are dealt once into a fixed hold-out split
(``bench.stats.stable_split``, 50/50, stratified answerable / unanswerable). Platt is fitted on
**dev** only; every reported column — including the shipped gate — is scored on **test**, so no
number this prints was fitted or chosen on the rows it is measured on.

Candidates score a ``FusedResult`` — the exact object production recall computes — and
``current`` *is* production's ``recall_confidence`` with the backend's ``get_calibration``, so
what is measured is what ships (a backend that can't judge scores ``NO_RECALL``: never ``ok``).
"""

from __future__ import annotations

import json
import random
import shutil
import statistics
import tempfile
from collections.abc import Callable
from pathlib import Path

from bench.backends import make_embedder, parse_spec
from bench.report import print_rows
from bench.stats import auroc, bootstrap_stat_ci, brier, ece, platt_fit, stable_split, wilson
from bench.stores import build_store
from core.domain.confidence import Calibration, calibrate, pool_z
from core.ports.embedding import EmbeddingGateway
from core.recall import (
    FusedResult,
    best_match,
    get_calibration,
    is_trusted,
    recall_confidence,
    search_fused_with_stats,
)
from core.store import Store

NO_RECALL = float("-inf")  # can never be `ok` (nothing returned, or unjudged); ranks below every real score
DEV_FRACTION = 0.5  # Platt has two parameters: half the queries fit it, half judge it

Candidate = Callable[[FusedResult, Calibration | None], float]


def _top1(result: FusedResult) -> float:
    return best_match(result.hits)[1]


def _judged(confidence: float | None) -> float:
    return NO_RECALL if confidence is None else confidence


CANDIDATES: dict[str, Candidate] = {
    # The shipped score: production's recall_confidence under the backend's calibration.
    "current": lambda result, calibration: _judged(recall_confidence(result, calibration)),
    # Absolute strength of the best match, no context.
    "top1": lambda result, _calibration: _top1(result),
    # Best match against the whole scanned pool — the signal `current` calibrates.
    "pool_z": lambda result, _calibration: pool_z(_top1(result), result.pool),
    # Mean returned similarity against the pool — evidence mass.
    "topk_z": lambda result, _calibration: pool_z(statistics.fmean(sim for _s, sim, _r in result.hits), result.pool),
}


def candidate_scores(result: FusedResult, calibration: Calibration | None) -> dict[str, float]:
    """Every candidate's raw score for one recall (``NO_RECALL`` when nothing came back)."""
    if not result.hits:
        return {name: NO_RECALL for name in CANDIDATES}
    return {name: fn(result, calibration) for name, fn in CANDIDATES.items()}


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
    """Run each ``(query, gold_texts)`` through production recall; label and score it, keeping the
    production confidence as returned (``None`` when unjudged) for the shipped gate. ``answerable``
    (gold exists) is the stratum the hold-out split keeps balanced."""
    calibration = get_calibration(embedder)
    out = []
    for query, gold in labelled:
        result = search_fused_with_stats(store, embedder, project, query, cfg, k=cfg.activated_k)
        returned = {row["text"] for _s, _sim, row in result.hits}
        out.append(
            {
                "q": query,
                "answerable": bool(gold),
                "label": bool(gold & returned),
                "confidence": recall_confidence(result, calibration),
                "scores": candidate_scores(result, calibration),
            }
        )
    return out


def split(observations: list[dict]) -> tuple[list[dict], list[dict]]:
    """The fixed ``(dev, test)`` hold-out of the labelled queries (keyed by query text)."""
    return stable_split(
        observations, key=lambda o: o["q"], dev_fraction=DEV_FRACTION, stratum=lambda o: o["answerable"]
    )


def fit_calibration(scores: list[float], labels: list[bool]) -> Calibration:
    """Platt fit over the rows that returned something (``NO_RECALL`` carries no score to fit)."""
    fit = [i for i, sc in enumerate(scores) if sc != NO_RECALL]
    return Calibration(*platt_fit([scores[i] for i in fit], [labels[i] for i in fit]))


def probabilities(scores: list[float], calibration: Calibration) -> list[float]:
    """Calibrated probabilities; a row with no recall maps to 0 (it can never be ``ok``)."""
    return [0.0 if sc == NO_RECALL else calibrate(sc, calibration) for sc in scores]


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
    """Per-candidate discrimination, calibration and gate metrics: Platt fitted on the dev split,
    every metric scored on the test split."""
    dev, test = split(observations)
    dev_labels = [o["label"] for o in dev]
    labels = [o["label"] for o in test]
    n = len(labels)
    by_name = {name: [o["scores"][name] for o in test] for name in CANDIDATES}
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
        calibration = fit_calibration([o["scores"][name] for o in dev], dev_labels)
        probs = probabilities(scores, calibration)
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
                "platt (a, b) [dev]": f"{calibration.a:.4f}, {calibration.b:.4f}",
            }
        )
    shipped = gate([is_trusted(o["confidence"], shipped_threshold) for o in test], labels)
    return {"dev_n": len(dev), "test_n": n, "positives": sum(labels), "rows": rows, "shipped": shipped}


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
    "platt (a, b) [dev]",
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
    summary.update(
        backend=spec,
        answerable=len(data["queries"]),
        unanswerable=len(labelled) - len(data["queries"]),
        observations=observations,
    )
    return summary


def write_observations(out: Path, spec: str, distractors: int, observations: list[dict]) -> None:
    """Append one JSON line per query — answerable, label, production confidence, every candidate's raw
    score (``None`` where ``NO_RECALL``) — so a calibration or threshold can be fitted offline, on the
    same hold-out split (``split`` keys on ``q`` and stratifies on ``answerable``)."""
    with out.open("a", encoding="utf-8") as fh:
        for o in observations:
            record = {"backend": spec, "distractors": distractors, "q": o["q"], "answerable": o["answerable"]}
            scores = {name: None if v == NO_RECALL else v for name, v in o["scores"].items()}
            fh.write(
                json.dumps({**record, "label": o["label"], "confidence": o["confidence"], "scores": scores}) + "\n"
            )


def run_confidence(
    data: dict,
    cfg,
    backends: list[str],
    distractors: list[tuple[str, float]],
    ok_precision: float,
    out: Path | None = None,
) -> None:
    for spec in backends:
        try:
            summary = evaluate_confidence(spec, data, cfg, distractors, ok_precision)
        except Exception as exc:
            print(f"[confidence skipped {spec}] {exc}")
            continue
        if out is not None:
            write_observations(out, spec, len(distractors), summary["observations"])
        print(
            f"\nRecall-verdict calibration — {spec}, k={cfg.activated_k}, {len(distractors)} distractors: "
            f"{summary['answerable']} answerable + {summary['unanswerable']} unanswerable queries — "
            f"Platt fitted on dev ({summary['dev_n']}), every column on test ({summary['test_n']}, "
            f"{summary['positives']} positive: gold returned)\n"
        )
        print_rows(summary["rows"], CONFIDENCE_COLS)
        shipped = summary["shipped"]
        print(
            f"\n  shipped gate on test (production `ok`: current >= recall_min_confidence {cfg.recall_min_confidence}): "
            f"ok_n {shipped['ok_n']}, "
            f"precision {_gate_cell(shipped['precision'], shipped['precision_ci'])}, "
            f"recall {_gate_cell(shipped['recall'], shipped['recall_ci'])}"
        )
        print(
            f"  ok precision / recall above are at calibrated p >= {ok_precision} (Platt fitted on dev, scored on test)"
        )
