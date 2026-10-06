"""Calibration-benchmark tests: candidates, labelling, the hold-out split, distractor mining,
snapshots, ledger replay.

All stdlib and deterministic (hash embedder, temp stores). The benchmark's numbers decide
the recall verdict's formula and threshold, so its plumbing is pinned here: candidates score
exactly what production recall computes, labels mean "gold returned", the calibration is fitted
on dev and every reported metric is scored on test, mined distractors can't leak benchmark labels
or private text, and the live store is only ever read via a snapshot.
"""

from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from _harness import temp_data_dir

from bench.confidence_eval import (
    CANDIDATES,
    NO_RECALL,
    candidate_scores,
    dataset_records,
    fit_calibration,
    gate,
    observe,
    probabilities,
    split,
    summarise,
    write_observations,
)
from bench.distractors import mine_distractors
from bench.replay_ledger import replay
from bench.snapshot import find_project, snapshot_db
from bench.stores import build_store
from core import service
from core.config import get_config
from core.domain.confidence import PoolStats
from core.ports.embedding import HashEmbedding
from core.recall import SEMANTIC_CALIBRATION, FusedResult, recall_confidence
from core.store import Store


def _row(fact_id: str, text: str) -> dict:
    return {"id": fact_id, "text": text}


class CandidateTests(unittest.TestCase):
    def setUp(self):
        hits = [
            (0.9, 0.70, _row("a", "newer weaker fact")),
            (0.8, 0.80, _row("b", "deployment runs on github actions")),
        ]
        self.result = FusedResult(hits, PoolStats(n=100, mean=0.60, std=0.05))

    def test_current_is_production_recall_confidence(self):
        self.assertEqual(
            candidate_scores(self.result, SEMANTIC_CALIBRATION)["current"],
            recall_confidence(self.result, SEMANTIC_CALIBRATION),
        )

    def test_an_unjudged_backend_scores_current_as_never_ok(self):
        self.assertEqual(candidate_scores(self.result, None)["current"], NO_RECALL)

    def test_pool_candidates_judge_the_best_match(self):
        scores = candidate_scores(self.result, SEMANTIC_CALIBRATION)
        self.assertAlmostEqual(scores["top1"], 0.80)
        self.assertAlmostEqual(scores["pool_z"], (0.80 - 0.60) / 0.05)
        self.assertAlmostEqual(scores["topk_z"], (0.75 - 0.60) / 0.05)

    def test_zero_spread_pool_scores_zero_not_divide(self):
        flat = FusedResult(self.result.hits, PoolStats(n=2, mean=0.75, std=0.0))
        self.assertEqual(candidate_scores(flat, SEMANTIC_CALIBRATION)["pool_z"], 0.0)

    def test_empty_recall_is_no_recall_for_every_candidate(self):
        self.assertEqual(
            candidate_scores(FusedResult([], PoolStats(0, 0.0, 0.0)), SEMANTIC_CALIBRATION),
            dict.fromkeys(CANDIDATES, NO_RECALL),
        )


class ObservationRecordTests(unittest.TestCase):
    def test_records_round_trip_with_no_recall_as_null(self):
        observations = [
            {"q": "a", "answerable": True, "label": True, "confidence": 0.62, "scores": dict.fromkeys(CANDIDATES, 1.5)},
            {
                "q": "b",
                "answerable": False,
                "label": False,
                "confidence": None,
                "scores": dict.fromkeys(CANDIDATES, NO_RECALL),
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "obs.jsonl"
            write_observations(out, "fastembed", 20000, observations)
            rows = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual(
            [(r["backend"], r["distractors"], r["q"], r["answerable"], r["label"]) for r in rows],
            [("fastembed", 20000, "a", True, True), ("fastembed", 20000, "b", False, False)],
        )
        self.assertEqual((rows[0]["confidence"], rows[1]["confidence"]), (0.62, None))
        self.assertEqual(rows[1]["scores"], dict.fromkeys(CANDIDATES))  # NO_RECALL → null, not -Infinity


class GateAndFitTests(unittest.TestCase):
    def test_gate_known_values(self):
        out = gate([True, True, False, False], [True, False, True, False])
        self.assertEqual((out["ok_n"], out["precision"], out["recall"]), (2, 0.5, 0.5))

    def test_gate_with_no_ok_has_undefined_precision(self):
        self.assertIsNone(gate([False, False], [True, False])["precision"])

    def test_no_recall_rows_are_not_fitted_and_map_to_zero(self):
        scores = [NO_RECALL, 0.1, 0.2, 0.3, 0.7, 0.8, 0.9, 0.85]
        labels = [True, False, False, False, True, True, True, True]  # the NO_RECALL row's label is ignored
        calibration = fit_calibration(scores, labels)
        self.assertEqual(calibration, fit_calibration(scores[1:], labels[1:]))
        probs = probabilities(scores, calibration)
        self.assertEqual(probs[0], 0.0)
        self.assertTrue(all(0.0 <= p <= 1.0 for p in probs))


def _observation(i: int, answerable: bool) -> dict:
    """A synthetic labelled query: answerable ones mostly score high and are mostly positive."""
    score = (i % 7) / 2 + (2.0 if answerable else 0.0)
    return {
        "q": f"query {i}",
        "answerable": answerable,
        "label": answerable and i % 3 != 0,
        "confidence": round(score / 6, 3),
        "scores": dict.fromkeys(CANDIDATES, score),
    }


class HoldOutSplitTests(unittest.TestCase):
    def setUp(self):
        self.observations = [_observation(i, answerable=i % 4 != 0) for i in range(80)]

    def test_split_is_disjoint_complete_stratified_and_order_free(self):
        dev, test = split(self.observations)
        self.assertEqual(len(dev) + len(test), len(self.observations))
        self.assertFalse({o["q"] for o in dev} & {o["q"] for o in test})
        self.assertEqual(sum(o["answerable"] for o in dev), sum(o["answerable"] for o in test))  # 60 → 30 / 30
        self.assertEqual(sum(not o["answerable"] for o in dev), 10)  # 20 → 10 / 10
        reversed_dev, _ = split(list(reversed(self.observations)))
        self.assertEqual({o["q"] for o in reversed_dev}, {o["q"] for o in dev})

    def test_calibration_is_fitted_on_dev_only(self):
        dev, test = split(self.observations)
        summary = summarise(self.observations, ok_precision=0.9, shipped_threshold=0.4)
        expected = fit_calibration([o["scores"]["pool_z"] for o in dev], [o["label"] for o in dev])
        row = next(r for r in summary["rows"] if r["candidate"] == "pool_z")
        self.assertEqual(row["platt (a, b) [dev]"], f"{expected.a:.4f}, {expected.b:.4f}")
        self.assertEqual((summary["dev_n"], summary["test_n"]), (len(dev), len(test)))
        self.assertEqual(summary["positives"], sum(o["label"] for o in test))

    def test_every_reported_metric_is_scored_on_test_only(self):
        dev_q = {o["q"] for o in split(self.observations)[0]}
        before = summarise(self.observations, ok_precision=0.9, shipped_threshold=0.4)
        # Flip every dev label: the dev fit moves, so only fit-derived cells may change; the shipped
        # gate and AUROC are computed from test rows alone and must not.
        flipped = [{**o, "label": not o["label"]} if o["q"] in dev_q else o for o in self.observations]
        after = summarise(flipped, ok_precision=0.9, shipped_threshold=0.4)
        self.assertEqual(before["shipped"], after["shipped"])
        self.assertEqual([r["auroc"] for r in before["rows"]], [r["auroc"] for r in after["rows"]])
        self.assertNotEqual(
            [r["platt (a, b) [dev]"] for r in before["rows"]], [r["platt (a, b) [dev]"] for r in after["rows"]]
        )


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_data_dir(self)
        self.cfg = replace(get_config(), activated_k=3)
        self.embedder = HashEmbedding(dim=self.cfg.dim)

    def test_build_store_stamps_dataset_facts_with_distractor_ages(self):
        facts = ["deployment runs on github actions", "frontend uses tailwind utility classes"]
        distractors = [("the viewer is served on localhost only", 1_000_000.0)]
        store, project = build_store(self.embedder, self.cfg, dataset_records(facts, distractors), Path(self.tmp.name))
        try:
            rows = store.active_rows_for_project(project["key"])
            self.assertEqual(len(rows), 3)
            self.assertEqual({r["created_at"] for r in rows}, {1_000_000.0})
        finally:
            store.close()

    def test_labels_mean_gold_returned(self):
        facts = [
            "The deployment pipeline runs on github actions with a manual approval gate.",
            "Frontend styling uses tailwind utility classes.",
        ]
        store, project = build_store(self.embedder, self.cfg, dataset_records(facts, []), Path(self.tmp.name))
        try:
            labelled = [
                ("deployment pipeline github actions", {facts[0]}),
                ("zebra migration patterns in the serengeti", set()),
            ]
            observations = observe(store, self.embedder, project, self.cfg, labelled)
        finally:
            store.close()
        self.assertEqual([o["label"] for o in observations], [True, False])
        self.assertEqual([o["answerable"] for o in observations], [True, False])
        self.assertEqual(set(observations[0]["scores"]), set(CANDIDATES))
        summary = summarise(observations, ok_precision=0.9, shipped_threshold=self.cfg.recall_min_confidence)
        self.assertEqual(summary["dev_n"] + summary["test_n"], 2)
        self.assertEqual([r["candidate"] for r in summary["rows"]], list(CANDIDATES))
        for row in summary["rows"]:  # the dev fit a shipped Calibration is taken from — finite even when tiny
            a, b = (float(v) for v in row["platt (a, b) [dev]"].split(","))
            self.assertTrue(all(map(math.isfinite, (a, b))))


class DistractorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = get_config()
        self.store = Store(Path(self.tmp.name) / "src.db")
        self.project = {"key": "srcproj", "path": "/work/repo", "label": "source-project"}
        embedder = HashEmbedding(dim=cfg.dim)
        facts = [
            "short one",  # too short
            "The benchmark recall@1 went up after the fusion change landed.",  # contaminated
            "Ping ops at someone@example.com before any production database change.",  # privacy
            "The deployment pipeline runs on github actions with a manual approval gate.",  # near-dup of dataset
            "Invoices are generated on the first working day of each calendar month.",
            "The mobile client caches the last twenty screens for offline reading.",
        ]
        service.add_facts(self.store, embedder, replace(cfg, supersede_threshold=1.0), self.project, "s1", facts)
        self.dataset = ["The deployment pipeline runs on github actions with a manual approval gate."]

    def tearDown(self):
        self.store.close()

    def test_filters_short_contaminated_private_and_near_duplicate(self):
        mined = mine_distractors(self.store, self.project, n=10, exclude=self.dataset)
        self.assertEqual(
            sorted(text for text, _ts in mined),
            [
                "Invoices are generated on the first working day of each calendar month.",
                "The mobile client caches the last twenty screens for offline reading.",
            ],
        )
        self.assertTrue(all(ts > 0 for _text, ts in mined))  # real timestamps carried through

    def test_respects_n_and_is_seeded(self):
        once = mine_distractors(self.store, self.project, n=1, exclude=self.dataset, seed=3)
        self.assertEqual(len(once), 1)
        self.assertEqual(once, mine_distractors(self.store, self.project, n=1, exclude=self.dataset, seed=3))

    def test_unknown_repo_path_flags_every_absolute_path(self):
        service.add_facts(
            self.store,
            HashEmbedding(dim=get_config().dim),
            get_config(),
            self.project,
            "s2",
            ["The config file lives at /Users/someone/private/settings.toml on the laptop."],
        )
        no_path = {**self.project, "path": ""}
        mined = mine_distractors(self.store, no_path, n=10, exclude=self.dataset)
        self.assertFalse(any("/Users/" in text for text, _ts in mined))

    def test_find_project_by_key_or_label(self):
        self.assertEqual(find_project(self.store, "srcproj")["label"], "source-project")
        self.assertEqual(find_project(self.store, "source-project")["key"], "srcproj")
        self.assertIsNone(find_project(self.store, "nope"))


class SnapshotTests(unittest.TestCase):
    def test_copy_is_readable_and_removed_on_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "live.db"
            con = sqlite3.connect(src)
            con.execute("CREATE TABLE t (x INTEGER)")
            con.execute("INSERT INTO t VALUES (42)")
            con.commit()
            con.close()
            with snapshot_db(src) as snap:
                self.assertNotEqual(snap, src)
                self.assertEqual(sqlite3.connect(snap).execute("SELECT x FROM t").fetchone()[0], 42)
            self.assertFalse(snap.parent.exists())
            self.assertTrue(src.exists())


class LedgerReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_data_dir(self)
        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "p1", "path": "/work/p1", "label": "p1"}
        service.add_facts(
            self.store, self.embedder, self.cfg, self.project, "s1", ["The deployment pipeline runs on github actions."]
        )
        for ts, query, verdict in (
            (1.0, "deployment pipeline", "low_confidence"),
            (2.0, "unknown thing", "no_memory"),
            (3.0, "github actions deploy", "ok"),
            (4.0, "deployment pipeline", "ok"),  # repeat: one entry, most recent position
        ):
            self.store.log_recall("p1", query, returned=1, top_sim=0.5, confidence=0.5, verdict=verdict, now=ts)

    def tearDown(self):
        self.store.close()

    def test_recent_recall_queries_distinct_newest_first_answered_only(self):
        self.assertEqual(
            self.store.recent_recall_queries(10), [("p1", "deployment pipeline"), ("p1", "github actions deploy")]
        )
        self.assertEqual(self.store.recent_recall_queries(1), [("p1", "deployment pipeline")])

    def test_replay_scores_every_candidate(self):
        out = replay(self.store, self.embedder, self.cfg, n=10, now=10.0)
        self.assertEqual(out["replayed"], 2)
        self.assertEqual({name: len(values) for name, values in out["scores"].items()}, dict.fromkeys(CANDIDATES, 2))
        self.assertEqual(out["demoted"], 0)  # a single fact is both best match and fused #1


if __name__ == "__main__":
    unittest.main()
