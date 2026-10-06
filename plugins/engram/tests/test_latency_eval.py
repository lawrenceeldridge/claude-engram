"""``engram eval --latency`` (bench/latency_eval.py) — the hot-path cost harness, on a fixture store."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from _harness import temp_data_dir

from bench import latency_eval
from bench.latency_eval import (
    CONSOLIDATION_STAGES,
    PATHS,
    SCORERS,
    PreEmbedded,
    evaluate_consolidation,
    evaluate_latency,
    instrument,
    run_latency,
)
from bench.snapshot import snapshot_project
from bench.stores import build_store
from core.config import get_config
from core.ports.embedding import HashEmbedding

FACTS = [
    "The deploy pipeline runs on GitHub Actions and ships to AWS Lambda.",
    "Recall injects facts through additionalContext on UserPromptSubmit.",
    "Embeddings are quantised to int8 so the store stays compact.",
    "The capture worker runs detached so a turn never waits on distillation.",
    "Consolidation runs at SessionEnd and PreCompact, like sleep.",
]
QUESTIONS = ["how do we deploy", "where does recall inject", "why int8 vectors"]


def _available_scorers() -> list[str]:
    return [s for s in SCORERS if latency_eval._scorer_available(s)]


class LatencyFixture(unittest.TestCase):
    """A source DB with one project's facts and answered ledger questions, outside the data dir."""

    def setUp(self):
        temp_data_dir(self)
        self.cfg = replace(get_config(), distiller="heuristic")
        src = tempfile.TemporaryDirectory()
        self.addCleanup(src.cleanup)
        store, _project = build_store(
            HashEmbedding(dim=self.cfg.dim), self.cfg, [(t, None) for t in FACTS], Path(src.name), "proj"
        )
        self.source = Path(store.path)
        for i, question in enumerate(QUESTIONS):
            store.log_recall("proj", question, returned=1, top_sim=0.5, confidence=0.5, verdict="ok", now=100.0 + i)
        store.log_recall(
            "other", "a question from another project", returned=1, top_sim=0.5, confidence=0.5, verdict="ok"
        )
        store.close()

    def _args(self, **overrides) -> argparse.Namespace:
        values = dict(
            store_db=self.source,
            store_project="proj",
            latency=True,
            latency_consolidation=False,
            latency_n=40,
            latency_python_n=2,
            latency_out=None,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def _fingerprint(self) -> tuple[str, int]:
        return hashlib.sha256(self.source.read_bytes()).hexdigest(), os.stat(self.source).st_mtime_ns


class EvaluateLatencyTests(LatencyFixture):
    def _run(self) -> dict:
        with snapshot_project(self.source, "proj") as (store, project):
            return evaluate_latency(store, project, HashEmbedding(dim=self.cfg.dim), self.cfg, 40, 2)

    def test_every_path_and_available_scorer_is_timed(self):
        summary = self._run()
        scorers = _available_scorers()
        self.assertEqual(
            {(r["path"], r["scorer"]) for r in summary["results"]}, {(p, s) for p in PATHS for s in scorers}
        )
        for row in summary["results"]:
            self.assertEqual(row["queries"], 2 if row["scorer"] == "python" else len(QUESTIONS))
            self.assertGreater(row["p50_ms"], 0.0)
            self.assertLessEqual(row["p50_ms"], row["max_ms"])
        self.assertEqual(summary["store"]["facts"], len(FACTS))

    def test_only_this_projects_ledger_questions_are_asked(self):
        with snapshot_project(self.source, "proj") as (store, _project):
            asked = [q for _k, q in store.recent_recall_queries(40, project_key="proj")]
        self.assertEqual(asked, list(reversed(QUESTIONS)))

    def test_stages_cover_each_path(self):
        summary = self._run()
        self.assertEqual(summary["stages_scorer"], _available_scorers()[0])
        self.assertEqual(set(summary["stages"]), set(PATHS))
        self.assertTrue({"load", "scan", "rank", "other"} <= set(summary["stages"]["hook"]))
        self.assertTrue({"load", "scan", "lexical", "fts", "pool", "fusion"} <= set(summary["stages"]["tool"]))

    def test_the_parity_digest_is_stable_across_runs_of_one_db(self):
        first = {(r["path"], r["scorer"]): r["digest"] for r in self._run()["results"]}
        second = {(r["path"], r["scorer"]): r["digest"] for r in self._run()["results"]}
        self.assertEqual(first, second)
        self.assertEqual(len(set(first.values())), len(first))  # the paths' outputs differ

    def test_a_project_with_no_answered_questions_is_refused(self):
        with snapshot_project(self.source, "proj") as (store, project):
            empty = {**project, "key": "nothing-asked"}
            with self.assertRaises(LookupError):
                evaluate_latency(store, empty, HashEmbedding(dim=self.cfg.dim), self.cfg, 40, 2)


class ConsolidationTimingTests(LatencyFixture):
    def test_every_stage_is_timed_with_its_count(self):
        with snapshot_project(self.source, "proj") as (store, project):
            summary = evaluate_consolidation(store, project, HashEmbedding(dim=self.cfg.dim), self.cfg)
        self.assertEqual([row["stage"] for row in summary["stages"]], [s[0] for s in CONSOLIDATION_STAGES])
        self.assertTrue(all(isinstance(row["changed"], int) for row in summary["stages"]))
        self.assertGreaterEqual(summary["total_ms"], sum(row["ms"] for row in summary["stages"]))


class RunLatencyTests(LatencyFixture):
    def _quiet(self, args) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_latency(self.cfg, ["hash"], args)
        return code, out.getvalue()

    def test_the_source_db_is_never_written(self):
        before = self._fingerprint()
        code, _ = self._quiet(self._args(latency_consolidation=True))  # consolidation writes — to its snapshot
        self.assertEqual(code, 0)
        self.assertEqual(self._fingerprint(), before)

    def test_writes_a_report(self):
        out = Path(self.source.parent) / "latency.json"
        code, printed = self._quiet(self._args(latency_out=out, latency_consolidation=True))
        self.assertEqual(code, 0)
        self.assertIn("digest hook/", printed)
        report = json.loads(out.read_text())
        self.assertEqual(set(report["backends"]["hash"]), {"latency", "consolidation"})

    def test_the_run_never_reaches_an_llm_distiller(self):
        seen = []

        def fake_consolidate(store, cfg, project, now=None, embedder=None):
            seen.append(cfg.distiller)
            return {key: 0 for *_, key in CONSOLIDATION_STAGES}

        with mock.patch.object(latency_eval, "consolidate", fake_consolidate), redirect_stdout(io.StringIO()):
            run_latency(replace(self.cfg, distiller="claude"), ["hash"], self._args(latency_consolidation=True))
        self.assertEqual(seen, ["heuristic"])  # integrate's LLM tier would otherwise call `claude -p`

    def test_bad_requests_are_refused_not_tracebacks(self):
        for args in (
            self._args(store_project=None),
            self._args(store_db=self.source.parent / "none.db"),
            self._args(store_project="no-such-project"),
        ):
            with self.subTest(args=args):
                self.assertEqual(self._quiet(args)[0], 1)

    def test_a_backend_that_cannot_read_the_stored_vectors_is_skipped(self):
        with redirect_stdout(io.StringIO()) as out:
            run_latency(self.cfg, ["hash+float"], self._args())
            run_latency(replace(self.cfg, dim=self.cfg.dim // 2), ["hash"], self._args())
        self.assertIn("+float ranks in memory", out.getvalue())
        self.assertIn("-dim queries, but the store holds", out.getvalue())


class HelperTests(unittest.TestCase):
    def test_pre_embedded_serves_queries_only(self):
        inner = HashEmbedding(dim=32)
        embedder = PreEmbedded(inner, ["q"])
        self.assertEqual(embedder.embed_query("q"), inner.embed_query("q"))
        self.assertEqual((embedder.dim, embedder.semantic), (inner.dim, inner.semantic))
        with self.assertRaises(NotImplementedError):
            embedder.embed(["anything"])

    def test_instrument_times_and_restores_every_kind_of_attribute(self):
        class Owner:
            def method(self):
                return "m"

            @staticmethod
            def static():
                return "s"

        originals = (Owner.__dict__["method"], Owner.__dict__["static"])
        stages = (("a", Owner, "method"), ("b", Owner, "static"))
        with instrument(stages) as totals:
            self.assertEqual((Owner().method(), Owner.static(), Owner().static()), ("m", "s", "s"))
        self.assertEqual(set(totals), {"a", "b"})
        self.assertEqual((Owner.__dict__["method"], Owner.__dict__["static"]), originals)


if __name__ == "__main__":
    unittest.main()
