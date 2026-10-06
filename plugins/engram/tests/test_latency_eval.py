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
    INDEX_PATHS,
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
from core.index.indexer import index_project
from core.ports.embedding import HashEmbedding
from core.recall import render_block, search
from core.service import PROMPT_MEMORY_HEADER

FACTS = [
    "The deploy pipeline runs on GitHub Actions and ships to AWS Lambda.",
    "Recall injects facts through additionalContext on UserPromptSubmit.",
    "Embeddings are quantised to int8 so the store stays compact.",
    "The capture worker runs detached so a turn never waits on distillation.",
    "Consolidation runs at SessionEnd and PreCompact, like sleep.",
]
QUESTIONS = ["how do we deploy", "where does recall inject", "why int8 vectors"]
# The project's indexed tree: one code symbol and two doc sections the questions reach.
SOURCES = {
    "deploy.py": 'def deploy_pipeline():\n    """How do we deploy: GitHub Actions ships to AWS Lambda."""\n',
    "NOTES.md": "# Notes\n\n## Where recall injects\n\nRecall injects facts on UserPromptSubmit.\n\n"
    "## Why int8 vectors\n\nInt8 vectors keep the store compact.\n",
}


def _available_scorers() -> list[str]:
    return [s for s in SCORERS if latency_eval._scorer_available(s)]


class LatencyFixture(unittest.TestCase):
    """A source DB with one project's facts, its indexed code/docs and answered ledger questions,
    outside the data dir."""

    def setUp(self):
        temp_data_dir(self)
        # The index gate off, so the index block's output doesn't hinge on hash-cosine tuning.
        self.cfg = replace(get_config(), distiller="heuristic", index_min_sim=-1.0)
        src = tempfile.TemporaryDirectory()
        self.addCleanup(src.cleanup)
        embedder = HashEmbedding(dim=self.cfg.dim)
        store, project = build_store(embedder, self.cfg, [(t, None) for t in FACTS], Path(src.name), "proj")
        for name, text in SOURCES.items():
            (Path(src.name) / name).write_text(text, encoding="utf-8")
        index_project(store, embedder, self.cfg, project, src.name)
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
    def _run(self, *, unindexed: bool = False) -> dict:
        with snapshot_project(self.source, "proj") as (store, project):
            if unindexed:  # the snapshot only — the source keeps its chunks
                with store.db:
                    store.db.execute("DELETE FROM chunks")
            with redirect_stdout(io.StringIO()):
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
        stages = summary["stages"]
        self.assertTrue({"load", "scan", "rank", "other"} <= set(stages["hook"]))
        self.assertTrue({"load", "scan", "lexical", "fts", "pool", "fusion"} <= set(stages["tool"]))
        self.assertTrue({"index_fts", "index_load"} <= set(stages["index"]))
        for path in ("code", "docs"):
            self.assertTrue({"index_load", "index_fts", "scan", "fusion", "freshness"} <= set(stages[path]), path)

    def test_a_stage_a_path_never_reaches_stays_out_of_its_row(self):
        stages = self._run()["stages"]
        self.assertFalse({"index_fts", "index_load", "freshness"} & set(stages["hook"]))
        self.assertFalse({"load", "fts", "pool"} & set(stages["code"]))

    def test_the_parity_digest_is_stable_across_runs_of_one_db(self):
        first = {(r["path"], r["scorer"]): r["digest"] for r in self._run()["results"]}
        second = {(r["path"], r["scorer"]): r["digest"] for r in self._run()["results"]}
        self.assertEqual(first, second)
        for scorer in _available_scorers():  # each path's output differs from every other's
            per_path = [digest for (_path, s), digest in first.items() if s == scorer]
            self.assertEqual(len(set(per_path)), len(PATHS), scorer)

    def test_the_hook_paths_report_what_they_inject(self):
        results = {(r["path"], r["scorer"]): r for r in self._run()["results"]}
        scorer = _available_scorers()[0]
        with snapshot_project(self.source, "proj") as (store, project):
            now = latency_eval._pinned_now(store, project)
            asked = [q for _k, q in store.recent_recall_queries(40, project_key="proj")]
            asked = asked[: results["hook", scorer]["queries"]]  # the pure-Python scorer runs a prefix
            embedder = PreEmbedded(HashEmbedding(dim=self.cfg.dim), asked)
            cfg = replace(self.cfg, scorer=scorer)
            blocks = [
                render_block(PROMPT_MEMORY_HEADER, search(store, embedder, project, q, cfg, now=now), cfg.max_chars)[0]
                for q in asked
            ]
        self.assertAlmostEqual(results["hook", scorer]["mean_chars"], sum(map(len, blocks)) / len(blocks))
        self.assertGreater(results["index", scorer]["mean_chars"], 0)  # the questions reach the indexed tree
        for path in ("tool", "code", "docs"):  # a tool's reply is requested, not injected
            self.assertIsNone(results[path, scorer]["mean_chars"], path)
        for row in results.values():
            self.assertTrue(0 <= row["hit_pct"] <= 100)
        self.assertEqual(results["code", scorer]["hit_pct"], 100)  # the code path answers every question

    def test_a_project_with_nothing_indexed_skips_the_index_paths(self):
        summary = self._run(unindexed=True)
        self.assertEqual({r["path"] for r in summary["results"]}, set(PATHS) - INDEX_PATHS)
        self.assertEqual(set(summary["stages"]), set(PATHS) - INDEX_PATHS)

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
        self.assertIn("digest index/", printed)
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
        for passage in (lambda: embedder.embed(["anything"]), lambda: embedder.embed_one("q")):
            with self.assertRaises(NotImplementedError):  # stored text is never embedded in a read
                passage()

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
