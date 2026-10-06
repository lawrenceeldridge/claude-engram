"""The detached capture worker (bin/capture.py) and consolidation at scale (#66 part 3).

Capture and consolidation hold separate single-flight locks, so a slow consolidation can never
stop a capture; each consolidation stage runs under a deadline; every best-effort step that fails
is recorded; and archival writes never overwrite another writer's verdict.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from _harness import temp_data_dir

import capture

from core import consolidation, errlog, service
from core.config import get_config
from core.ports.embedding import HashEmbedding
from core.store import Store

TURNS = [
    ("user", "how does the deploy pipeline work?"),
    ("assistant", "We decided to deploy with GitHub Actions to AWS Lambda for every merge to main."),
    ("user", "and the vectors?"),
    ("assistant", "Embeddings are quantised to int8 so the store stays compact and recall stays fast."),
]


class CaptureWorkerTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(temp_data_dir(self).name)
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        (self.work / ".git").mkdir()
        self.transcript = self.work / "t.jsonl"
        self.transcript.write_text(
            "".join(
                json.dumps({"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}})
                + "\n"
                for role, text in TURNS
            ),
            encoding="utf-8",
        )

    def _run(self, event: str = "Stop") -> None:
        payload = self.work / f"payload-{event}.json"
        payload.write_text(
            json.dumps(
                {
                    "transcript_path": str(self.transcript),
                    "cwd": str(self.work),
                    "session_id": "s1",
                    "hook_event_name": event,
                }
            ),
            encoding="utf-8",
        )
        capture._run_worker(str(payload))

    def _facts(self) -> int:
        store = Store(get_config().db_path)
        try:
            return store.count()
        finally:
            store.close()

    def test_a_capture_completes_while_another_process_consolidates(self):
        (self.data / ".consolidate.lock").write_text("1")  # a live process (pid 1) is mid-consolidation
        with mock.patch.object(consolidation, "consolidate") as consolidate:
            self._run("SessionEnd")
        self.assertGreater(self._facts(), 0)  # captured regardless
        consolidate.assert_not_called()  # skipped: the running pass owns consolidation
        self.assertFalse((self.data / ".capture.lock").exists())

    def test_a_held_capture_lock_makes_the_worker_yield(self):
        (self.data / ".capture.lock").write_text("1")
        self._run()
        self.assertEqual(self._facts(), 0)

    def test_a_checkpoint_consolidates_once_and_releases_both_locks(self):
        with mock.patch.object(consolidation, "consolidate", wraps=consolidation.consolidate) as consolidate:
            self._run("SessionEnd")
            self._run("Stop")  # not a checkpoint: no consolidation
        self.assertEqual(consolidate.call_count, 1)
        self.assertFalse((self.data / ".capture.lock").exists())
        self.assertFalse((self.data / ".consolidate.lock").exists())

    def test_a_capture_rebuilds_an_emptied_keyword_index_first_and_says_so(self):
        self._run()  # a store with facts
        store = Store(get_config().db_path)
        with store.db:
            store.db.execute(
                "INSERT INTO facts_fts(facts_fts) VALUES ('delete-all')"
            )  # what an interrupted replay left
        rows = store.count()
        self.assertEqual(store.fts_coverage()["facts_fts"], (0, rows))
        store.close()
        self._run()
        store = Store(get_config().db_path)
        self.addCleanup(store.close)
        indexed, total = store.fts_coverage()["facts_fts"]
        self.assertEqual(indexed, total)
        store.db.execute("INSERT INTO facts_fts(facts_fts, rank) VALUES ('integrity-check', 1)")  # raises if not
        self.assertIn(f"fts repair: facts_fts indexed 0 of {rows:,} rows — rebuilt", errlog.last(self.data)["message"])

    def test_a_failing_step_is_recorded_not_raised(self):
        with mock.patch.object(service, "capture_transcript_incremental", side_effect=RuntimeError("disk full")):
            self._run()
        event = errlog.last(self.data)
        self.assertEqual(event["source"], "capture")
        self.assertIn("transcript", event["message"])
        self.assertIn("disk full", event["message"])


class StageDeadlineTests(unittest.TestCase):
    def setUp(self):
        temp_data_dir(self)
        self.cfg = replace(get_config(), distiller="heuristic")
        self.store = Store(self.cfg.db_path)
        self.addCleanup(self.store.close)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}

    def test_the_deadline_interrupts_a_long_statement_and_the_connection_stays_usable(self):
        endless = "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT COUNT(*) FROM r"
        with self.assertRaises(sqlite3.OperationalError) as caught:
            with self.store.deadline(0.05):
                self.store.db.execute(endless).fetchone()
        self.assertIn("interrupted", str(caught.exception))
        self.assertEqual(tuple(self.store.db.execute("SELECT 1").fetchone()), (1,))
        self.assertEqual(
            tuple(self.store.db.execute("SELECT COUNT(*) FROM facts").fetchone()), (0,)
        )  # no deadline left behind

    def test_a_stage_past_its_deadline_is_counted_logged_and_the_pass_goes_on(self):
        def runaway_refine(store, cfg, project, now):
            store.db.execute(
                "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT COUNT(*) FROM r"
            ).fetchone()

        with (
            mock.patch.object(consolidation, "STAGE_DEADLINE_SECONDS", 0.05),
            mock.patch("core.consolidation.refine.refine", runaway_refine),
            mock.patch("core.consolidation.invalidate.invalidate_stale_antipatterns", return_value=7) as later_stage,
        ):
            counts = consolidation.consolidate(self.store, self.cfg, self.project)
        self.assertEqual((counts["interrupted"], counts["pruned"]), (1, 0))
        self.assertEqual(counts["invalidated"], 7)  # the stages after it still ran
        later_stage.assert_called_once()
        event = errlog.last(self.cfg.data_dir)
        self.assertEqual(event["source"], "consolidate")
        self.assertIn("pruned", event["message"])


class GuardedArchiveTests(unittest.TestCase):
    """Capture and consolidation now write concurrently: archiving only touches active facts."""

    def setUp(self):
        temp_data_dir(self)
        self.cfg = replace(get_config(), distiller="heuristic")
        self.store = Store(self.cfg.db_path)
        self.addCleanup(self.store.close)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}
        embedder = HashEmbedding(dim=self.cfg.dim)
        for text in ("The deploy pipeline uses GitHub Actions.", "Embeddings are quantised to int8."):
            service.add_facts(self.store, embedder, self.cfg, self.project, "s1", [text])
        self.old, self.new = [r["id"] for r in self.store.active_rows_for_project("p")]
        self.store.supersede([self.old], self.new)  # a capture superseded `old` mid-pass

    def test_a_superseded_fact_keeps_its_status(self):
        self.assertEqual(self.store.set_status([self.old, self.new], "pruned"), 1)  # only the active one
        self.assertEqual(self.store.get(self.old)["status"], "superseded")
        self.assertEqual(self.store.get(self.old)["superseded_by"], self.new)

    def test_displacement_skips_an_already_archived_fact(self):
        self.store.db.execute("UPDATE facts SET status = 'active' WHERE id = ?", (self.new,))
        self.assertEqual(self.store.displace_stm("p", 0), 0)  # capacity 0 = off
        self.assertEqual(self.store.displace_stm("p", 1) + self.store.displace_stm("p", 1), 0)  # 1 active ≤ capacity
        self.assertEqual(self.store.get(self.old)["status"], "superseded")

    def test_invalidating_an_archived_fact_counts_zero(self):
        self.assertEqual(service.invalidate_facts(self.store, "p", [self.old]), 0)
        self.assertEqual(self.store.get(self.old)["status"], "superseded")
        self.assertEqual(service.invalidate_facts(self.store, "p", [self.new]), 1)


if __name__ == "__main__":
    unittest.main()
